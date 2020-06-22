import datetime
import os

from dateutil.tz import tzutc
from pyspark.sql import functions as F

from baskerville.db import get_jdbc_url
from baskerville.db.models import RequestSet
from baskerville.models.config import BaskervilleConfig
from baskerville.models.feature_manager import FeatureManager
from baskerville.models.request_set_cache import RequestSetSparkCache
from baskerville.spark.helpers import reset_spark_storage
from baskerville.spark.schemas import rs_cache_schema
from baskerville.util.helpers import Borg, TimeBucket, get_logger, \
    instantiate_from_str, FOLDER_CACHE


class ServiceProvider(Borg):
    """
    Provides the following services:
    - spark session
    - request set cache
    - db connection / db tools
    - ml related stuff: model, model index, feature manager
    """
    def __init__(self, config: BaskervilleConfig):
        self.config = config
        self.start_time = datetime.datetime.utcnow()
        self.runtime = None
        self.request_set_cache = None
        self.spark = None
        self.tools = None
        self.request_set_cache = None
        self.model = None
        self.model_index = None
        self.feature_manager = None
        self._can_predict = False
        self.spark_conf = self.config.spark
        self.db_url = get_jdbc_url(self.config.database)
        self.time_bucket = TimeBucket(self.config.engine.time_bucket)

        self.cache_columns = [
            'target',
            'ip',
            'first_ever_request',
            'old_subset_count',
            'old_features',
            'old_num_requests',
        ]
        self.cache_config = {
            'db_url': self.db_url,
            'db_driver': self.spark_conf.db_driver,
            'user': self.config.database.user,
            'password': self.config.database.password
        }
        self.logger = get_logger(
            self.__class__.__name__,
            logging_level=self.config.engine.log_level,
            output_file=self.config.engine.logpath
        )

    def create_runtime(self):
        self.runtime = self.tools.create_runtime(
            start=self.start_time,
            conf=self.config.engine
        )
        self.logger.info(f'Created runtime {self.runtime.id}')

    def initialize_spark_service(self):
        if not self.spark:
            from baskerville.spark import get_or_create_spark_session
            self.spark = get_or_create_spark_session(self.spark_conf)

    def initialize_request_set_cache_service(self):
        if not isinstance(self.request_set_cache, RequestSetSparkCache):
            self.request_set_cache = RequestSetSparkCache(
                cache_config=self.cache_config,
                table_name=RequestSet.__tablename__,
                columns_to_keep=(
                    'target',
                    'ip',
                    F.col('start').alias('first_ever_request'),
                    F.col('subset_count').alias('old_subset_count'),
                    F.col('features').alias('old_features'),
                    F.col('num_requests').alias('old_num_requests'),
                    F.col('updated_at')
                ),
                expire_if_longer_than=self.config.engine.cache_expire_time,
                path=os.path.join(self.config.engine.storage_path,
                                  FOLDER_CACHE)
            )
            if self.config.engine.cache_load_past:
                self.request_set_cache = self.request_set_cache.load(
                    update_date=(self.start_time - datetime.timedelta(
                        seconds=self.config.engine.cache_expire_time)
                    ).replace(tzinfo=tzutc()),
                    extra_filters=(
                        F.col('time_bucket') == self.time_bucket.sec
                    )  # todo: & (F.col("id_runtime") == self.runtime.id)?
                )
            else:
                self.request_set_cache.load_empty(rs_cache_schema)

            self.logger.info(f'In cache: {self.request_set_cache.count()}')

            return self.request_set_cache

    def initialize_db_tools_service(self):
        if not self.tools:
            from baskerville.util.baskerville_tools import BaskervilleDBTools
            self.tools = BaskervilleDBTools(self.config.database)
            self.tools.connect_to_db()

    def initialize_model_service(self):
        if not self.model:
            if self.config.engine.model_id:
                self.model_index = self.tools.get_ml_model_from_db(
                    self.config.engine.model_id)
                self.model = instantiate_from_str(self.model_index.algorithm)
                self.model.load(bytes.decode(
                    self.model_index.classifier, 'utf8'), self.spark)
                if not self.feature_manager:
                    self.initialize_feature_manager_service()
                self._can_predict = self.feature_manager.\
                    feature_config_is_valid() and self.model.iforest_model
                self.model_index.can_predict = self.feature_manager. \
                    feature_config_is_valid()
            else:
                self.model = None

        self._can_predict = self.feature_manager\
                                .feature_config_is_valid() and self.model

    def initialize_feature_manager_service(self):
        if not self.feature_manager:
            self.feature_manager = FeatureManager(self.config.engine)
            self.feature_manager.initialize()

    def initalize_ml_services(self):
        self.initialize_feature_manager_service()
        self.initialize_model_service()

    def refresh_cache(self, df):
        """
        Update the cache with the current batch of logs in logs_df and clean up
        :return:
        """
        self.request_set_cache.update_self(df)
        df.unpersist()
        df = None
        # self.spark.catalog.clearCache()

    def filter_cache(self, df):
        """
        Use the current logs to find the past request sets - if any - in the
        request set cache
        :return:
        """
        df = df.select(
            F.col('client_request_host').alias('target'),
            F.col('client_ip').alias('ip'),
        ).distinct().alias('a').persist(self.spark_conf.storage_level)

        self.request_set_cache.filter_by(df)

        df.unpersist()
        del df

    def add_cache_columns(self, df):
        """
        Add columns from the cache to facilitate the
        feature extraction, prediction, and save processes
        :return:
        :rtype: pyspark.sql.DataFrame
        """
        df = df.alias('df')
        self.filter_cache(df)
        df = self.request_set_cache.update_df(
            df, select_cols=self.cache_columns
        )
        self.logger.debug(
            f'****** > # of rows in cache: {self.request_set_cache.count()}')
        return df

    def finish_up(self):
        """
        Unpersist all
        :return:
        """
        reset_spark_storage()

    def reset(self):
        """
        Unpersist rdds and dataframes and call GC - see broadcast memory
        release issue
        :return:
        """
        import gc

        reset_spark_storage()
        gc.collect()
