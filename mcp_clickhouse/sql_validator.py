"""SQL query validation using sqlglot for parsing and analysis."""

import logging
from typing import Optional, List, Tuple
from datetime import datetime, timedelta
import re
import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


class SQLValidationError(Exception):
    """Exception raised for SQL validation errors."""
    pass


def has_limit_clause(query: str) -> bool:
    """Check if a SQL query contains a LIMIT clause using sqlglot AST parsing.
    
    Args:
        query: The SQL query string to check
        
    Returns:
        True if LIMIT clause is found, False otherwise
    """
    try:
        # Parse the query using ClickHouse dialect
        parsed = sqlglot.parse_one(query, dialect="clickhouse")
        
        # Check if the query has a LIMIT clause
        limit_node = parsed.find(exp.Limit)
        
        return limit_node is not None
    except Exception as e:
        logger.warning(f"Failed to parse query with sqlglot: {e}. Falling back to False.")
        return False


def has_time_range_filter(query: str, max_range_minutes: Optional[int] = None) -> Tuple[bool, Optional[str]]:
    """Check if a SQL query contains time range filtering in WHERE clause.
    
    Args:
        query: The SQL query string to check
        max_range_minutes: Maximum allowed time range in minutes (e.g., 30 for 30 minutes).
                          If None, only checks for presence of time filter without validating range.
        
    Returns:
        Tuple of (has_time_filter, message) where:
        - has_time_filter: True if time range filter is found and within allowed range
        - message: Optional descriptive message about what was found or missing
    """
    time_columns = [
           'Timestamp', 'timestamp'
    ]
    
    try:
        parsed = sqlglot.parse_one(query, dialect="clickhouse")
        
        where_clauses = list(parsed.find_all(exp.Where))
        
        if not where_clauses:
            return False, "No WHERE clause found in query"
        
        found_time_filter = False
        time_column_found = None
        time_expressions = []
        
        for where in where_clauses:
            comparisons = list(where.find_all(exp.Binary, exp.Between))
            
            for comparison in comparisons:
                columns = list(comparison.find_all(exp.Column))
                
                for col in columns:
                    col_name = col.name.lower()
                    
                    if any(time_col.lower() in col_name for time_col in time_columns):
                        found_time_filter = True
                        time_column_found = col.name
                        time_expressions.append(comparison)
                        break
        
        if not found_time_filter:
            return False, f"No time range filter found. Consider adding filters on columns like: {', '.join(time_columns[:5])}"
        
        # If max_range_minutes is specified, validate the time range
        if max_range_minutes is not None:
            range_valid, range_message = _validate_time_range(time_expressions, max_range_minutes)
            if not range_valid:
                return False, range_message
            return True, f"Time range filter found on column: {time_column_found}. {range_message}"
        
        return True, f"Time range filter found on column: {time_column_found}"
            
    except Exception as e:
        logger.warning(f"Failed to parse query with sqlglot: {e}")
        return False, f"Could not parse query to check for time filters: {str(e)}"


def _validate_time_range(time_expressions: List, max_range_minutes: int) -> Tuple[bool, str]:
    """Validate that the time range in expressions doesn't exceed the maximum allowed.
    
    Args:
        time_expressions: List of AST nodes containing time comparisons
        max_range_minutes: Maximum allowed time range in minutes
        
    Returns:
        Tuple of (is_valid, message)
    """
    try:
        # Extract time values from expressions
        start_time = None
        end_time = None
        
        for expr in time_expressions:
            expr_sql = expr.sql(dialect="clickhouse")
            
            # Handle BETWEEN expressions
            if isinstance(expr, exp.Between):
                low = expr.args.get('low')
                high = expr.args.get('high')
                if low and high:
                    start_val = _extract_time_value(low.sql(dialect="clickhouse"))
                    end_val = _extract_time_value(high.sql(dialect="clickhouse"))
                    if start_val and end_val:
                        start_time = start_val
                        end_time = end_val
            
            # Handle comparison operators (>=, >, <, <=, =)
            elif isinstance(expr, exp.Binary):
                left = expr.left
                right = expr.right
                op = expr.key
                
                # Determine which side is the time value
                time_val = None
                if isinstance(right, (exp.Literal, exp.DataType)):
                    time_val = _extract_time_value(right.sql(dialect="clickhouse"))
                
                if time_val:
                    # >= or > suggests start time
                    if op in ('gte', 'gt'):
                        start_time = time_val
                    # <= or < suggests end time
                    elif op in ('lte', 'lt'):
                        end_time = time_val
        
        if start_time and end_time:
            time_diff = end_time - start_time
            diff_minutes = time_diff.total_seconds() / 60
            
            if diff_minutes > max_range_minutes:
                return False, f"Time range ({diff_minutes:.0f} minutes) exceeds maximum allowed ({max_range_minutes} minutes)"
            else:
                return True, f"Time range validated ({diff_minutes:.0f} minutes <= {max_range_minutes} minutes)"
        
        for expr in time_expressions:
            expr_sql = expr.sql(dialect="clickhouse").upper()
            if 'INTERVAL' in expr_sql:
                interval_minutes = _extract_interval_minutes(expr_sql)
                if interval_minutes and interval_minutes > max_range_minutes:
                    return False, f"INTERVAL expression ({interval_minutes} minutes) exceeds maximum allowed ({max_range_minutes} minutes)"
                elif interval_minutes:
                    return True, f"Time range validated (INTERVAL {interval_minutes} minutes <= {max_range_minutes} minutes)"
        
        logger.warning("Could not extract time range values for validation")
        return True, "Time filter present but range could not be validated"
        
    except Exception as e:
        logger.warning(f"Error validating time range: {e}")
        return True, f"Time filter present but range validation failed: {str(e)}"


def _extract_time_value(value_str: str) -> Optional[datetime]:
    """Extract datetime value from a string literal.
    
    Args:
        value_str: String representation of a time value
        
    Returns:
        datetime object or None if parsing fails
    """
    # Remove quotes and whitespace
    value_str = value_str.strip().strip("'\"")
    
    # Common datetime formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(value_str, fmt)
        except ValueError:
            continue
    
    return None


def _extract_interval_minutes(expr_sql: str) -> Optional[int]:
    """Extract minutes from an INTERVAL expression.
    
    Args:
        expr_sql: SQL expression containing INTERVAL
        
    Returns:
        Number of minutes or None if not parseable
    """
    # Match patterns like "INTERVAL 30 MINUTE", "INTERVAL 1 HOUR", "INTERVAL 1 DAY"
    patterns = [
        (r'INTERVAL\s+(\d+)\s+MINUTE', 1),
        (r'INTERVAL\s+(\d+)\s+HOUR', 60),
        (r'INTERVAL\s+(\d+)\s+DAY', 1440),
    ]
    
    for pattern, multiplier in patterns:
        match = re.search(pattern, expr_sql, re.IGNORECASE)
        if match:
            return int(match.group(1)) * multiplier
    
    return None


def validate_query(query: str, max_range_minutes: int = 30) -> Tuple[bool, List[str]]:
    """Validate a SQL query for LIMIT clause and time range filtering.
    
    Args:
        query: The SQL query string to validate
        max_range_minutes: Maximum allowed time range in minutes (default: 30)
        
    Returns:
        Tuple of (is_valid, error_messages) where:
        - is_valid: True if query passes all validations
        - error_messages: List of validation error messages (empty if valid)
    """
    errors = []
    
    if not has_limit_clause(query):
        errors.append("Query must include a LIMIT clause to prevent accidentally retrieving too many rows")
    
    has_time_filter, message = has_time_range_filter(query, max_range_minutes)
    if not has_time_filter:
        errors.append(f"Query should include a time range filter. {message}")
    
    return len(errors) == 0, errors
