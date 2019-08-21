"""
This allows to compare events coming from two different ETL runs to highlight differences in elapsed time or row counts.

* Pre-requisites

You need to have a list of events for each ETL. Arthur can provide this using the "query_events" command.
For example:
```
arthur.py query_events -p development 37ACEC7440AB4620 -q > 37ACEC7440AB4620.events
arthur.py query_events -p development 96BE11B234F84F39 -q > 96BE11B234F84F39.events
```

* Usage

Then you can compare those two ETLs using:
```
compare_events.py 37ACEC7440AB4620.events 96BE11B234F84F39.events
```

(These should be "older ETL events" before "newer ETL events".
"""

import csv
import re
import sys
from collections import defaultdict
from math import isclose

from tabulate import tabulate


_last_row_re = re.compile(r'\(\d+\s*rows\)')
_column_spacing = re.compile(r'\s+\|\s+')


def read_file(filename):
    """Read output from query_events command, which must contain "elapsed" and "rowcount" columns."""
    print(f"Reading events from {filename}")
    with open(filename) as f:
        for i, line in enumerate(f.readlines()):
            if i != 1 and not _last_row_re.match(line):
                yield _column_spacing.sub('|', line).strip()


def parse_file(filename):
    """Parse the input as |-delimited columns."""
    lines = read_file(filename)
    for row in csv.DictReader(lines, delimiter='|'):
        yield row


def extract_values(filename):
    """Find elapsed time and rowcount for each target relation."""

    def default_value():
        return {'elapsed': None, 'rowcount': None}

    values = defaultdict(default_value)
    values.update(
        (
            (
                (row['step'], row['target']),
                {
                    'elapsed': float(row['elapsed']) if row['elapsed'] != '---' else None,
                    'rowcount': int(row['rowcount']) if row['rowcount'] != '---' else None,
                },
            )
            for row in parse_file(filename)
        )
    )
    return values


def delta(a, b):
    """Return change in percent (or None if undefined)"""
    if a is None or b is None:
        return None
    if a == .0 and b == .0:
        return .0
    assert a != .0 and b != .0
    return round((b - a) * 1000.0 / a) / 10.0


def show_delta(previous, current, column):
    previous_value = previous.get(column)
    current_value = current.get(column)

    if previous_value is None or current_value is None:
        return False
    if previous_value == current_value:
        return False

    if column == 'elapsed':
        if current_value < 10.0:
            return False

        return not isclose(previous_value, current_value, rel_tol=0.1)  # , abs_tol=1000)

    if column == 'rowcount':
        if previous_value > current_value:
            return True
        if previous_value < 1000 or current_value < 1000:
            return previous_value != current_value

    return not isclose(previous_value, current_value, rel_tol=0.1)  # , abs_tol=1000)


def print_table(previous_values, current_values, column):
    events = frozenset(previous_values).union(current_values)
    table = sorted(
        [
            (
                target,
                step,
                previous_values[(step, target)][column],
                current_values[(step, target)][column],
                delta(previous_values[(step, target)][column], current_values[(step, target)][column]),
            )
            for step, target in sorted(events)
            if show_delta(previous_values[(step, target)], current_values[(step, target)], column)
        ],
        key=lambda row: (row[0], row[1]),
        reverse=True,
    )
    print(tabulate(table, headers=('target', 'step', 'prev. ' + column, 'cur. ' + column, 'delta %'), tablefmt='psql'))


def main():
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} previous_events current_events', file=sys.stderr)
        sys.exit(1)

    previous_events, current_events = sys.argv[1:3]

    previous_values = extract_values(previous_events)
    current_values = extract_values(current_events)

    print_table(previous_values, current_values, 'elapsed')
    print()
    print_table(previous_values, current_values, 'rowcount')


if __name__ == "__main__":
    main()
