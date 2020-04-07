import logging
import os.path
from typing import Dict, List

from etl.config.dw import DataWarehouseSchema
from etl.extract.extractor import Extractor
from etl.relation import RelationDescription


class StaticExtractor(Extractor):

    """
    Enable using files in S3 as an upstream data source.
    """

    # TODO Describe expected file paths, existence of "_SUCCESS" file

    def __init__(
        self,
        schemas: Dict[str, DataWarehouseSchema],
        relations: List[RelationDescription],
        keep_going: bool,
        dry_run: bool,
    ) -> None:
        # For static sources, we go straight to failure when the success file does not exist
        super().__init__("static", schemas, relations, keep_going, needs_to_wait=False, dry_run=dry_run)
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _current_location(source: DataWarehouseSchema, relation: RelationDescription):
        return os.path.join(source.s3_path_prefix, relation.csv_path_name)

    @staticmethod
    def source_info(source: DataWarehouseSchema, relation: RelationDescription):
        return {
            "name": source.name,
            "bucket_name": source.s3_bucket,
            "object_prefix": StaticExtractor._current_location(source, relation),
        }

    def extract_table(self, source: DataWarehouseSchema, relation: RelationDescription):
        """
        Render the S3 path template for a given source to check for data files before writing
        out a manifest file
        """
        bucket = source.s3_bucket
        prefix = self._current_location(source, relation)
        self.write_manifest_file(relation, bucket, prefix)
