# Copyright (c) 2015. Mount Sinai School of Medicine
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function, division, absolute_import

import logging
from os.path import join, exists

import datacache
from typechecks import require_integer, require_string

from .common import CACHE_SUBDIR
from .locus import normalize_chromosome, normalize_strand, Locus

# any time we update the database schema, increment this version number
DATABASE_SCHEMA_VERSION = 2

class Database(object):
    """
    Wrapper around sqlite3 database so that the rest of the
    library doesn't have to worry about constructing the .db file or
    writing SQL queries directly.
    """

    def __init__(self, gtf, auto_download=False):
        self.gtf = gtf
        self.auto_download = auto_download
        self._connection = None

        self.logger = logging.getLogger()
        self.logger.setLevel(logging.INFO)

    def __eq__(self, other):
        return isinstance(other, Database) and self.gtf == other.gtf

    def local_db_filename(self):
        base = self.gtf.base_filename()
        return base + ".db"

    def local_db_path(self):
        dirpath = self.gtf.local_dir()
        filename = self.local_db_filename()
        return join(dirpath, filename)

    def _all_possible_indices(self, column_names):
        """
        Create list of tuples containing all possible index groups
        we might want to create over tables in this database.

        If a release is missing some column we want to index on,
        we have to drop any indices which use that column.

        A specific table may later drop some of these indices if they're
        missing  values for that feature or are the same as the table's primary key.
        """
        candidate_column_groups = [
            ['seqname', 'start', 'end'],
            ['seqname', 'start', 'end', 'strand'],
            ['gene_name'],
            ['gene_id'],
            ['transcript_id'],
            ['transcript_name'],
            ['exon_id'],
            ['protein_id'],
            ['ccds_id'],
        ]
        indices = []
        column_set = set(column_names)
        # Since queries are often restricted by feature type
        # we should include that column in combination with all
        # other indices we anticipate might improve performance
        for column_group in candidate_column_groups:
            skip = False
            for column_name in column_group:
                # some columns, such as 'exon_id',
                # are not available in all releases of Ensembl
                if column_name not in column_set:
                    logging.info(
                        "Skipping database index for {%s}",
                        ", ".join(column_group))
                    skip = True
            if skip:
                continue
            indices.append(column_group)
        return indices

    # mapping from database tables to their primary keys
    # sadly exon IDs *are* not unique, so can't be in this dict
    PRIMAY_KEY_COLUMNS = {
        'CDS': 'protein_id',
        'gene': 'gene_id',
        'transcript': 'transcript_id'
    }

    def _get_primary_key(self, feature_name, feature_df):
        """Name of primary key for a feature table (e.g. "gene" -> "gene_id")

        Since we're potentially going to run this code over unseen data,
        make sure that the primary is unique and never null.

        If a feature doesn't have a primary key, return None.
        """
        if feature_name not in self.PRIMAY_KEY_COLUMNS:
            return None
        primary_key = self.PRIMAY_KEY_COLUMNS[feature_name]
        primary_key_values = feature_df[primary_key]
        if primary_key_values.isnull().any():
            raise ValueError(
                "Column '%s' can't be primary key of table '%s'"
                " because it contains nulls values" % (
                    primary_key, feature_name))
        elif len(primary_key_values.unique()) < len(primary_key_values):
            raise ValueError(
                "Column '%s' can't be primary key of table '%s'"
                " because it contains repeated values" % (
                    primary_key, feature_name))
        else:
            return primary_key

    def _feature_indices(self, all_index_groups, primary_key, feature_df):
        """Choose subset ofindex group tuples from `all_index_groups` which are
        applicable to a particular feature (not same as its primary key, have
        non-null values).
        """
        # each feature only gets indices if they're *not* the
        # primary key and have non-null values in the feature's
        # subset of data
        result = []
        for index_group in all_index_groups:
            # is the index group just a primary key?
            if len(index_group) == 1 and index_group[0] == primary_key:
                continue
            index_column_values = feature_df[index_group]
            if len(index_column_values.dropna()) == 0:
                continue
            result.append(index_group)
        return result

    def _create_database(self, force=False):
        print("Creating database: %s" % self.local_db_path())
        filename = self.local_db_filename()
        df = self.gtf.dataframe()

        all_index_groups = self._all_possible_indices(df.columns)

        # split single DataFrame into dictionary mapping each unique
        # feature name onto that subset of the data
        feature_names = df['feature'].unique()
        dataframes = {}
        # every table gets the same set of indices
        indices_dict = {}
        # if a feature has an ID then make it that table's primary key
        primary_keys = {}

        for feature in feature_names:
            df_subset = df[df.feature == feature]
            dataframes[feature] = df_subset

            primary_key = self._get_primary_key(feature, df_subset)
            if primary_key:
                primary_keys[feature] = primary_key

            indices_dict[feature] = self._feature_indices(
                all_index_groups,
                primary_key,
                df_subset)

        self._connection = datacache.db_from_dataframes(
            db_filename=filename,
            dataframes=dataframes,
            indices=indices_dict,
            primary_keys=primary_keys,
            subdir=CACHE_SUBDIR,
            overwrite=force,
            version=DATABASE_SCHEMA_VERSION)
        return self._connection

    def _connect_if_exists(self):
        """
        Return the connection if the DB exists, and otherwise return
        None. As a side effect, stores the database connection in
        self._connection.
        """
        if self._connection is None:
            db_path = self.local_db_path()
            if exists(db_path):
                # since version metadata is filled in last in datacache,
                # checking for a version also implicitly checks for
                # having finishing fill in all the tables/rows.
                #
                # TODO: expose this more explicitly in datacache
                #
                self._connection = datacache.connect_if_correct_version(
                    db_path, DATABASE_SCHEMA_VERSION)
        return self._connection

    @property
    def connection(self):
        """
        Return the sqlite3 database for this Ensembl release
        (download and/or construct it if necessary, if auto_download
        is on). As a side effect, stores the database connection in
        self._connection.
        """
        connection = self._connect_if_exists()
        if connection:
            return connection
        if self.auto_download:
            return self._create_database()
        raise ValueError("Genome annotation data is not currently "
                         "installed for release %s. Run "
                         "\"pyensembl install %s\" or call "
                         "EnsemblRelease(%s).install()" %
                         ((self.gtf.release,) * 3))

    def columns(self, table_name):
        sql = "PRAGMA table_info(%s)" % table_name
        table_info = self.connection.execute(sql).fetchall()
        return [info[1] for info in table_info]

    def column_exists(self, table_name, column_name):
        return column_name in self.columns(table_name)

    def column_values_at_locus(
            self,
            column_name,
            feature,
            contig,
            position,
            end=None,
            strand=None,
            distinct=False,
            sorted=False):
        """
        Get the non-null values of a column from the database
        at a particular range of loci
        """

        # TODO: combine with the query method, since they overlap
        # significantly
        require_string(column_name, "column_name", nonempty=True)

        contig = normalize_chromosome(contig)

        require_integer(position, "position")

        if end is None:
            end = position

        require_integer(end, "end")

        if not self.column_exists(feature, column_name):
            raise ValueError("Table %s doesn't have column %s" % (
                feature, column_name,))

        if distinct:
            distinct_string = "DISTINCT "
        else:
            distinct_string = ""

        query = """
            SELECT %s%s
            FROM %s
            WHERE seqname= ?
            AND start <= ?
            AND end >= ?

        """ % (distinct_string, column_name, feature)

        query_params = [contig, end, position]

        if strand:
            query += " AND strand = ?"
            query_params.append(strand)

        tuples = self.connection.execute(query, query_params).fetchall()

        # each result is a tuple, so pull out its first element
        results = [t[0] for t in tuples if t[0] is not None]

        if sorted:
            results.sort()
        return results

    def distinct_column_values_at_locus(
            self,
            column,
            feature,
            contig,
            position,
            end=None,
            strand=None):
        """
        Gather all the distinct values for a property/column at some specified
        locus.

        Parameters
        ----------
        column : str
            Which property are we getting the values of.

        feature : str
            Which type of entry (e.g. transcript, exon, gene) is the property
            associated with?

        contig : str
            Chromosome or unplaced contig name

        position : int
            Chromosomal position

        end : int, optional
            End position of a range, if unspecified assume we're only looking
            at the single given position.

        strand : str, optional
            Either the positive ('+') or negative strand ('-'). If unspecified
            then check for values on either strand.
        """
        return self.column_values_at_locus(
            column,
            feature,
            contig,
            position,
            end=end,
            strand=strand,
            distinct=True,
            sorted=True)

    def run_sql_query(self, sql, required=False, query_params=[]):
        """
        Given an arbitrary SQL query, run it against the Ensembl database
        and return the results.
        """
        cursor = self.connection.execute(sql, query_params)
        results = cursor.fetchall()
        if required and not results:
            raise ValueError(
                "No results found in Ensembl for query:\n%s" % (sql,))

        return results

    def query(
            self,
            select_column_names,
            filter_column,
            filter_value,
            feature,
            distinct=False,
            required=False):
        """
        Construct a SQL query and run against the ensembl sqlite3 database,
        filtered both by the feature type and a user-provided column/value.
        """
        sql = """
            SELECT %s%s
            FROM %s
            WHERE %s = ?
        """ % ("distinct " if distinct else "",
               ", ".join(select_column_names),
               feature,
               filter_column)
        query_params = [filter_value]
        return self.run_sql_query(
            sql, required=required, query_params=query_params)

    def query_one(
            self,
            select_column_names,
            filter_column,
            filter_value,
            feature,
            distinct=False,
            required=False):
        results = self.query(
            select_column_names,
            filter_column,
            filter_value,
            feature,
            distinct=distinct,
            required=required)

        if len(results) == 0:
            raise ValueError("%s not found: %s" % (filter_column, filter_value))
        elif len(results) > 1:
            raise ValueError(
                "Found multiple entries with %s=%s (%s)" % (
                    filter_column, filter_value, results))
        return results[0]

    def query_feature_values(
            self,
            column,
            feature,
            distinct=True,
            contig=None,
            strand=None):
        """
        Run a SQL query against the Ensembl sqlite3 database, filtered
        only on the feature type.
        """
        query = """
            SELECT %s%s
            FROM %s
            WHERE 1=1
        """ % ("DISTINCT " if distinct else "", column, feature)
        query_params = []

        if contig:
            contig = normalize_chromosome(contig)
            query += " AND seqname = ?"
            query_params.append(contig)

        if strand:
            strand = normalize_strand(strand)
            query += " AND strand = ?"
            query_params.append(strand)

        rows = self.run_sql_query(query, query_params=query_params)
        return [row[0] for row in rows if row is not None]

    def query_distinct_on_contig(self, column_name, feature, contig):
        return self.query_feature_values(
            column=column_name,
            feature=feature,
            contig=contig,
            distinct=True)

    def query_loci(self, filter_column, filter_value, feature):
        """
        Query for loci satisfying a given filter and feature type.


        Parameters
        ----------
        filter_column : str
            Name of column to filter results by.

        filter_value : str
            Only return loci which have this value in the their filter_column.

        feature : str
            Feature names such as 'transcript', 'gene', and 'exon'

        Returns list of Locus objects
        """
        # list of values containing (contig, start, stop, strand)
        result_tuples = self.query(
            select_column_names=["seqname", "start", "end", "strand"],
            filter_column=filter_column,
            filter_value=filter_value,
            feature=feature,
            distinct=True,
            required=True)
        return [
            Locus(contig, start, end, strand)
            for (contig, start, end, strand)
            in result_tuples
        ]

    def query_locus(self, filter_column, filter_value, feature):
        """
        Query for unique locus, raises error if missing or more than
        one locus in the database.

        Parameters
        ----------
        filter_column : str
            Name of column to filter results by.

        filter_value : str
            Only return loci which have this value in the their filter_column.

        feature : str
            Feature names such as 'transcript', 'gene', and 'exon'

        Returns single Locus object.
        """
        loci = self.query_loci(
            filter_column=filter_column,
            filter_value=filter_value,
            feature=feature)

        if len(loci) == 0:
            raise ValueError("Couldn't find locus for %s with %s = %s" % (
                feature, filter_column, filter_value))
        elif len(loci) > 1:
            raise ValueError("Too many loci for %s with %s = %s: %s" % (
                feature, filter_column, filter_value, loci))
        return loci[0]

    def create(self, force=False):
        """
        Create the local database (including indexing) if it's not
        already set up. If `force` is True, always re-create
        the database from scratch.

        Returns True if the database was re-created.

        Raises an error if the necessary data is not yet downloaded.
        """
        if not force and self._connect_if_exists():
            return False
        self._create_database(force=force)
        return True
