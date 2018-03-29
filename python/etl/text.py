"""
Deal with text, mostly around iterables of texts which we want to pretty print.
Should not import Arthur modules (so that etl.errors remains widely importable)
"""

import textwrap

from tabulate import tabulate


def join_with_quotes(names):
    """
    Individually wrap the names in quotes and return comma-separated names in a string.

    If the input is a set of names, the names are sorted first.
    If the input is a list of names, the order of the list is respected.
    If the input is cheese, the order is for more red wine.

    >>> join_with_quotes(["foo", "bar"])
    "'foo', 'bar'"
    >>> join_with_quotes({"foo", "bar"})
    "'bar', 'foo'"
    >>> join_with_quotes(frozenset(["foo", "bar"]))
    "'bar', 'foo'"
    """
    if isinstance(names, (set, frozenset)):
        return ', '.join("'{}'".format(name) for name in sorted(names))
    else:
        return ', '.join("'{}'".format(name) for name in names)


def join_column_list(columns):
    """
    Return string with comma-separated, delimited column names
    """
    return ", ".join('"{}"'.format(column) for column in columns)


class ColumnWrapper(textwrap.TextWrapper):
    """
    Unlike the TextWrapper, we don't care about words and just treat the entire column as a chunk.
    """

    def _split(self, text):
        """
        Create one chunk smaller than the column width or three chunks with the last one wider than the placeholder.

        >>> cw = ColumnWrapper(width=10, placeholder='..')
        >>> cw._split("ciao")
        ['ciao']
        >>> cw._split("good morning")
        ['good mo', ' ', '???']
        """
        chunk = text.rstrip()
        if len(chunk) > self.width:
            return [chunk[:self.width - len(self.placeholder) - 1], ' ', '?' * (len(self.placeholder) + 1)]
        else:
            return [chunk]


def format_lines(value_rows, header_row=None, has_header=False, max_column_width=100) -> str:
    """
    Format a list of rows which each have a list of values, optionally with a header.

    >>> print(format_lines([["aa", "b", "ccc"], ["a", "bb", "c"]]))
     col #1   | col #2   | col #3
    ----------+----------+----------
     aa       | b        | ccc
     a        | bb       | c
    (2 rows)
    >>> print(format_lines([["name", "breed"], ["monty", "spaniel"], ["cody", "poodle"], ["cooper", "shepherd"]],
    ...                    has_header=True))
     name   | breed
    --------+----------
     monty  | spaniel
     cody   | poodle
     cooper | shepherd
    (3 rows)
    >>> print(format_lines([["windy"]], header_row=["weather"]))
     weather
    -----------
     windy
    (1 row)
    >>> print(format_lines([["some long line", "second column"]], max_column_width=6))
     col #1   | col #2
    ----------+----------
     some     | second
     long     | column
     line     |
    >>> print(format_lines([]))
    (0 rows)
    >>> format_lines([["a", "b"], ["c"]])
    Traceback (most recent call last):
    ValueError: unexpected row length: got 1, expected 2
    """
    if header_row is not None and has_header is True:
        raise ValueError("cannot have separate header row and mark first row as header")

    # Make sure that we are working with a list of lists of strings (and not generators and such).
    wrapper = ColumnWrapper(width=max_column_width,
                            expand_tabs=True, replace_whitespace=True, drop_whitespace=False)
    matrix = [[wrapper.fill(str(column)) for column in row] for row in value_rows]

    n_columns = len(matrix[0]) if len(matrix) > 0 else 0
    for i, row in enumerate(matrix):
        if len(row) != n_columns:
            raise ValueError("unexpected row length: got {:d}, expected {:d}".format(len(row), n_columns))

    if header_row:
        n_rows = len(matrix)
        headers = header_row
    elif has_header:
        n_rows = len(matrix) - 1
        headers = "firstrow"
    else:
        n_rows = len(matrix)
        headers = ["col #{:d}".format(i + 1) for i in range(n_columns)]
    row_count = "({:d} {})".format(n_rows, "row" if n_rows == 1 else "rows")
    if n_rows:
        lines = tabulate(matrix, headers=headers, tablefmt="presto")
        return lines + '\n' + row_count
    else:
        return row_count
