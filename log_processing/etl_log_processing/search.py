"""
Interactive use -- feed filenames (either local or remote on S3), output will be all matching log lines.

The "interactive" use is really more of a demonstration  / debugging utility.
The real juice comes from milking lambda connected to S3 events so that any
log file posted by the data pipelines is automatically drained into an
Elasticsearch Service pool. That should quench your thirst for log fluids.
"""

import sys
from functools import partial

# from etl_log_processing import upload
from . import upload


def print_message(record):
    """Example callback function which simply only prints the timestamp and the message of the log record."""
    print("{0[timestamp]} {0[etl_id]} {0[log_level]} {0[message]}".format(record))


def filter_record(query, record):
    for key in ("etl_id", "log_level", "message"):
        if query in record[key]:
            return True
    return False


def main():
    if len(sys.argv) < 3:
        print("Usage: {} QUERY LOGFILE [LOGFILE ...]".format(sys.argv[0]))
        exit(1)
    query = str(sys.argv[1])
    processed = upload.load_records(sys.argv[2:])
    matched = filter(partial(filter_record, query), processed)
    for record in sorted(matched, key=lambda r: r["datetime"]["epoch_time"]):
        print_message(record)


if __name__ == "__main__":
    main()
