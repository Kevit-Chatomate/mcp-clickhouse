"""SQL query validation using sqlglot for parsing and analysis."""

import logging
from typing import Optional, List, Tuple
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
        # In sqlglot, LIMIT is represented as a Limit node in the AST
        limit_node = parsed.find(exp.Limit)
        
        return limit_node is not None
    except Exception as e:
        logger.warning(f"Failed to parse query with sqlglot: {e}. Falling back to False.")
        return False


def has_time_range_filter(query: str, time_columns: Optional[List[str]] = None) -> Tuple[bool, Optional[str]]:
    """Check if a SQL query contains time range filtering in WHERE clause.
    
    Args:
        query: The SQL query string to check
        time_columns: Optional list of column names to check for time filtering.
                     If None, looks for common time column names like 'timestamp', 
                     'created_at', 'updated_at', 'event_time', 'datetime', 'date', 'time'
        
    Returns:
        Tuple of (has_time_filter, message) where:
        - has_time_filter: True if time range filter is found
        - message: Optional descriptive message about what was found or missing
    """
    if time_columns is None:
        # Common time-related column names (case-insensitive)
        time_columns = [
           'Timestamp', 'timestamp'
        ]
    
    try:
        # Parse the query using ClickHouse dialect
        parsed = sqlglot.parse_one(query, dialect="clickhouse")
        
        # Find all WHERE clauses
        where_clauses = list(parsed.find_all(exp.Where))
        
        if not where_clauses:
            return False, "No WHERE clause found in query"
        
        # Look for time-related comparisons in WHERE clause
        found_time_filter = False
        time_column_found = None
        
        for where in where_clauses:
            # Find all comparison operations (=, <, >, <=, >=, BETWEEN, etc.)
            comparisons = list(where.find_all(exp.Binary, exp.Between))
            
            for comparison in comparisons:
                # Extract column references from the comparison
                columns = list(comparison.find_all(exp.Column))
                
                for col in columns:
                    col_name = col.name.lower()
                    
                    # Check if this is a time-related column
                    if any(time_col.lower() in col_name for time_col in time_columns):
                        found_time_filter = True
                        time_column_found = col.name
                        break
                
                if found_time_filter:
                    break
            
            if found_time_filter:
                break
        
        if found_time_filter:
            return True, f"Time range filter found on column: {time_column_found}"
        else:
            return False, f"No time range filter found. Consider adding filters on columns like: {', '.join(time_columns[:5])}"
            
    except Exception as e:
        logger.warning(f"Failed to parse query with sqlglot: {e}")
        return False, f"Could not parse query to check for time filters: {str(e)}"


def validate_query(query: str, require_limit: bool = True, require_time_range: bool = False,
                   time_columns: Optional[List[str]] = None) -> Tuple[bool, List[str]]:
    """Validate a SQL query for LIMIT clause and optionally time range filtering.
    
    Args:
        query: The SQL query string to validate
        require_limit: Whether to require a LIMIT clause (default: True)
        require_time_range: Whether to require time range filtering (default: False)
        time_columns: Optional list of time column names to check for filtering
        
    Returns:
        Tuple of (is_valid, error_messages) where:
        - is_valid: True if query passes all validations
        - error_messages: List of validation error messages (empty if valid)
    """
    errors = []
    
    if require_limit and not has_limit_clause(query):
        errors.append("Query must include a LIMIT clause to prevent accidentally retrieving too many rows")
    
    if require_time_range:
        has_time_filter, message = has_time_range_filter(query, time_columns)
        if not has_time_filter:
            errors.append(f"Query should include a time range filter. {message}")
    
    return len(errors) == 0, errors
