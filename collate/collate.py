# -*- coding: utf-8 -*-
from itertools import product, chain
from functools import reduce
import sqlalchemy.sql.expression as ex
from sqlalchemy.ext.compiler import compiles


def make_list(a):
    return [a] if not isinstance(a, list) else a


def make_tuple(a):
    return (a,) if not isinstance(a, tuple) else a


def make_sql_clause(s, constructor):
    if not isinstance(s, ex.ClauseElement):
        return constructor(s)
    else:
        return s


class CreateTableAs(ex.Executable, ex.ClauseElement):

    def __init__(self, name, query):
        self.name = name
        self.query = query


@compiles(CreateTableAs)
def _create_table_as(element, compiler, **kw):
    return "CREATE TABLE %s AS %s" % (
        element.name,
        compiler.process(element.query)
    )


def to_sql_name(name):
    return name.replace('"', '')


class AggregateExpression(object):
    def __init__(self, aggregates, operator, cast=None):
        self.aggregates = aggregates
        self.operator = operator
        self.cast = cast if cast else ""

    def get_columns(self, when=None, prefix=None):
        if prefix is None:
            prefix = ""

        columns0 = self.aggregates[0].get_columns(when)
        columns1 = self.aggregates[1].get_columns(when)

        for c0, c1 in product(columns0, columns1):
            c = ex.literal_column("({}{} {} {})".format(
                    c0, self.cast, self.operator, c1))
            yield c.label("{}{}{}{}".format(
                    prefix, c0.name, self.operator, c1.name))

    # TODO: floordiv and truediv for py3
    def __add__(self, other):
        return AggregateExpression([self, other], "+")

    def __sub__(self, other):
        return AggregateExpression([self, other], "-")

    def __mul__(self, other):
        return AggregateExpression([self, other], "*")

    def __div__(self, other):
        return AggregateExpression([self, other], "/", "*1.0")


class Aggregate(AggregateExpression):
    """
    An object representing one or more SQL aggregate columns in a groupby
    """
    def __init__(self, quantity, function, order=None):
        """
        Args:
            quantity: SQL for the quantity to aggregate
            function: SQL aggregate function
            order: SQL for order by clause in an ordered set aggregate

        Notes:
            quantity, function, and order can also be lists of the above,
            in which case the cross product of those is used. If quantity is a
            collection than name should also be a collection of the same length.

            quantity can be a tuple of SQL quantities for aggregate functions
            that take multiple arguments, e.g. corr, regr_slope

            quantity can be a dictionary in which case the keys are names
            for the expressions and values are expressions.
        """
        if isinstance(quantity, dict):
            self.quantities = quantity
        else:
            # first convert to list of tuples
            quantities = [make_tuple(q) for q in make_list(quantity)]
            # then dict with name keys
            self.quantities = {to_sql_name(str.join("_", q)): q for q in quantities}

        self.functions = make_list(function)
        self.orders = make_list(order)

    def get_columns(self, when=None, prefix=None):
        """
        Args:
            when: used in a case statement to filter the rows going into the
                aggregation function
            prefix: prefix for column names
        Returns:
            collection of SQLAlchemy columns
        """
        if prefix is None:
            prefix = ""

        name_template = "{prefix}{quantity_name}_{function}"
        column_template = "{function}({args})"
        arg_template = "{quantity}"
        order_template = ""

        if self.orders != [None]:
            column_template += " WITHIN GROUP (ORDER BY {order_clause})"
            order_template = "CASE WHEN {when} THEN {order} END" if when else "{order}"
        elif when:
            arg_template = "CASE WHEN {when} THEN {quantity} END"

        for function, (quantity_name, quantity), order in product(
                self.functions, self.quantities.items(), self.orders):
            args = str.join(", ", (arg_template.format(when=when, quantity=q)
                                   for q in make_tuple(quantity)))
            order_clause = order_template.format(when=when, order=order)

            format_kwargs = dict(function=function, args=args, prefix=prefix,
                                 order_clause=order_clause,
                                 quantity_name=quantity_name)

            column = column_template.format(**format_kwargs)
            name = name_template.format(**format_kwargs)

            yield ex.literal_column(column).label(to_sql_name(name))


class SpacetimeAggregation(object):
    def __init__(self, aggregates, group_intervals, from_obj, dates,
                 prefix=None, suffix=None, date_column=None):
        """
        Args:
            aggregates: collection of Aggregate objects
            from_obj: defines the from clause, e.g. the name of the table
            group_intervals: a dictionary of group : intervals pairs where
                group is an expression by which to group and
                intervals is a collection of datetime intervals, e.g.
                {"address_id": ["1 month", "1 year]}
            dates: list of PostgreSQL date strings,
                e.g. ["2012-01-01", "2013-01-01"]
            prefix: prefix for column names, defaults to from_obj
            suffix: suffix for aggregation table, defaults to "aggregation"
            date_column: name of date column in from_obj, defaults to "date"

        The from_obj and group arguments are passed directly to the
            SQLAlchemy Select object so could be anything supported there.
            For details see:
            http://docs.sqlalchemy.org/en/latest/core/selectable.html
        """
        self.aggregates = aggregates
        self.from_obj = make_sql_clause(from_obj, ex.table)
        self.group_intervals = group_intervals
        self.groups = group_intervals.keys()
        self.dates = dates
        self.prefix = prefix if prefix else str(from_obj)
        self.suffix = suffix if suffix else "aggregation"
        self.date_column = date_column if date_column else "date"

    def _get_aggregates_sql(self, interval, date, group):
        """
        Helper for getting aggregates sql
        Args:
            interval: SQL time interval string, or "all"
            date: SQL date string
            group: group clause, for naming columns
        Returns: collection of aggregate column SQL strings
        """
        if interval != 'all':
            when = "{date_column} >= '{date}'::date - interval '{interval}'".format(
                    interval=interval, date=date, date_column=self.date_column)
        else:
            when = None

        prefix = "{prefix}_{group}_{interval}_".format(
                prefix=self.prefix, interval=interval,
                group=group)

        return chain(*(a.get_columns(when, prefix) for a in self.aggregates))

    def get_selects(self):
        """
        Constructs select queries for this aggregation

        Returns: a dictionary of group : queries pairs where
            group are the same keys as group_intervals
            queries is a list of Select queries, one for each date in dates
        """
        queries = {}

        for group, intervals in self.group_intervals.items():
            queries[group] = []
            for date in self.dates:
                columns = [group,
                           ex.literal_column("'%s'::date"
                                             % date).label("date")]
                columns += list(chain(*(self._get_aggregates_sql(
                        i, date, group) for i in intervals)))

                # upper bound on date_column by date
                where = ex.text("{date_column} < '{date}'".format(
                        date_column=self.date_column, date=date))

                gb_clause = make_sql_clause(group, ex.literal_column)
                query = ex.select(columns=columns, from_obj=self.from_obj)\
                          .where(where)\
                          .group_by(gb_clause)

                if 'all' not in intervals:
                    greatest = "greatest(%s)" % str.join(
                            ",", ["interval '%s'" % i for i in intervals])
                    query = query.where(ex.text(
                        "{date_column} >= '{date}'::date - {greatest}".format(
                            date_column=self.date_column, date=date,
                            greatest=greatest)))

                queries[group].append(query)

        return queries

    def _get_table_name(self, group):
        """
        Returns name for table for the given group
        """
        return '"%s"' % to_sql_name("%s_%s" % (self.prefix, group))

    def get_creates(self, selects=None):
        """
        Construct create queries for this aggregation
        Args:
            selects: the dictionary of select queries to use
                if None, use self.get_selects()
                this allows you to customize select queries before creation

        Returns:
            a dictionary of group : create pairs where
                group are the same keys as group_intervals
                create is a CreateTableAs object
        """
        if not selects:
            selects = self.get_selects()

        selects = {group: reduce(lambda s, t: s.union_all(t), sels)
                   for group, sels in selects.items()}

        return {group: CreateTableAs(self._get_table_name(group), select)
                for group, select in selects.items()}

    def get_drops(self):
        """
        Generate drop queries for this aggregation

        Returns: a dictionary of group : drop pairs where
            group are the same keys as group_intervals
            drop is a raw drop table query for the corresponding table
        """
        return {group: "DROP TABLE IF EXISTS %s;" % self._get_table_name(group)
                for group in self.groups}

    def get_indexes(self):
        """
        Generate create index queries for this aggregation

        Returns: a dictionary of group : index pairs where
            group are the same keys as group_intervals
            index is a raw create index query for the corresponding table
        """
        return {group: "CREATE INDEX ON %s (%s, %s);" %
                (self._get_table_name(group), group, "date")
                for group in self.groups}

    def get_join_table(self):
        """
        Generate a query for a join table
        """
        return ex.Select(columns=self.groups, from_obj=self.from_obj)\
                 .group_by(*self.groups)

    def get_create(self, join_table=None):
        """
        Generate a single aggregation table creation query by joining
            together the results of get_creates()
        Returns: a CREATE TABLE AS query
        """
        if not join_table:
            join_table = '(%s) t1' % self.get_join_table()

        name = "%s_%s" % (self.prefix, self.suffix)

        query = ("SELECT * FROM %s\n"
                 "CROSS JOIN (select unnest('{%s}'::date[]) as date) t2\n") % (
                join_table, str.join(',', self.dates))
        for group in self.groups:
            query += "LEFT JOIN %s USING (%s, date)" % (
                    self._get_table_name(group), group)

        return "CREATE TABLE %s AS (%s);" % (name, query)

    def get_drop(self):
        """
        Generate a drop table statement for the aggregation table
        Returns: string sql query
        """
        name = "%s_%s" % (self.prefix, self.suffix)
        return "DROP TABLE IF EXISTS %s" % name

    def execute(self, conn):
        """

        """
        creates = self.get_creates()
        drops = self.get_drops()
        indexes = self.get_indexes()

        trans = conn.begin()
        for group in self.groups:
            conn.execute(drops[group])
            conn.execute(creates[group])
            conn.execute(indexes[group])

        conn.execute(self.get_drop())
        conn.execute(self.get_create())
        trans.commit()
