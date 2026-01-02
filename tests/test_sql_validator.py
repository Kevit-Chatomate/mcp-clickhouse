"""Tests for SQL validation using sqlglot."""

import unittest
from mcp_clickhouse.sql_validator import (
    has_limit_clause,
    has_time_range_filter,
    validate_query,
    SQLValidationError
)


class TestSQLValidator(unittest.TestCase):
    """Test SQL validation functions."""

    def test_has_limit_clause_simple(self):
        """Test basic LIMIT clause detection."""
        # Queries with LIMIT
        self.assertTrue(has_limit_clause("SELECT * FROM table LIMIT 10"))
        self.assertTrue(has_limit_clause("SELECT * FROM table LIMIT 100"))
        self.assertTrue(has_limit_clause("select * from table limit 5"))
        
        # Queries without LIMIT
        self.assertFalse(has_limit_clause("SELECT * FROM table"))
        self.assertFalse(has_limit_clause("SELECT COUNT(*) FROM table"))

    def test_has_limit_clause_complex(self):
        """Test LIMIT clause detection in complex queries."""
        # With WHERE clause
        self.assertTrue(has_limit_clause(
            "SELECT * FROM table WHERE id > 100 LIMIT 50"
        ))
        
        # With ORDER BY
        self.assertTrue(has_limit_clause(
            "SELECT * FROM table ORDER BY created_at DESC LIMIT 20"
        ))
        
        # With JOINs
        self.assertTrue(has_limit_clause(
            "SELECT a.*, b.name FROM table1 a JOIN table2 b ON a.id = b.id LIMIT 10"
        ))
        
        # With GROUP BY
        self.assertTrue(has_limit_clause(
            "SELECT category, COUNT(*) FROM table GROUP BY category LIMIT 5"
        ))
        
        # Subquery with LIMIT in outer query
        self.assertTrue(has_limit_clause(
            "SELECT * FROM (SELECT * FROM table WHERE x > 5) LIMIT 10"
        ))

    def test_has_limit_clause_offset(self):
        """Test LIMIT clause with OFFSET."""
        self.assertTrue(has_limit_clause("SELECT * FROM table LIMIT 10 OFFSET 20"))
        self.assertTrue(has_limit_clause("SELECT * FROM table LIMIT 10, 20"))  # MySQL style

    def test_has_time_range_filter_basic(self):
        """Test basic time range filter detection."""
        # Queries with time filters
        has_filter, msg = has_time_range_filter(
            "SELECT * FROM table WHERE timestamp > '2024-01-01' LIMIT 10"
        )
        self.assertTrue(has_filter)
        self.assertIn("timestamp", msg.lower())
        
        has_filter, msg = has_time_range_filter(
            "SELECT * FROM table WHERE created_at >= '2024-01-01' AND created_at < '2024-02-01' LIMIT 10"
        )
        self.assertTrue(has_filter)
        
        # Queries without time filters
        has_filter, msg = has_time_range_filter(
            "SELECT * FROM table WHERE id > 100 LIMIT 10"
        )
        self.assertFalse(has_filter)

    def test_has_time_range_filter_various_columns(self):
        """Test time range filter detection with various column names."""
        time_related_queries = [
            "SELECT * FROM logs WHERE timestamp > now() - INTERVAL 1 HOUR LIMIT 10",
            "SELECT * FROM users WHERE created_at >= '2024-01-01' LIMIT 10",
            "SELECT * FROM events WHERE event_time BETWEEN '2024-01-01' AND '2024-01-02' LIMIT 10",
            "SELECT * FROM records WHERE updated_at > '2024-01-01' LIMIT 10",
            "SELECT * FROM data WHERE datetime >= toDateTime('2024-01-01') LIMIT 10",
            "SELECT * FROM table WHERE date = '2024-01-01' LIMIT 10",
        ]
        
        for query in time_related_queries:
            has_filter, msg = has_time_range_filter(query)
            self.assertTrue(has_filter, f"Failed to detect time filter in: {query}")

    def test_has_time_range_filter_custom_columns(self):
        """Test time range filter detection with custom column names."""
        # Provide custom time column names
        has_filter, msg = has_time_range_filter(
            "SELECT * FROM table WHERE my_custom_timestamp > '2024-01-01' LIMIT 10",
            time_columns=['my_custom_timestamp']
        )
        self.assertTrue(has_filter)
        
        # Should not detect if custom column is not in the list
        has_filter, msg = has_time_range_filter(
            "SELECT * FROM table WHERE other_timestamp > '2024-01-01' LIMIT 10",
            time_columns=['my_custom_timestamp']
        )
        self.assertFalse(has_filter)

    def test_has_time_range_filter_no_where(self):
        """Test queries without WHERE clause."""
        has_filter, msg = has_time_range_filter("SELECT * FROM table LIMIT 10")
        self.assertFalse(has_filter)
        self.assertIn("No WHERE clause", msg)

    def test_validate_query_limit_only(self):
        """Test query validation requiring only LIMIT."""
        # Valid query with LIMIT
        is_valid, errors = validate_query(
            "SELECT * FROM table LIMIT 10",
            require_limit=True,
            require_time_range=False
        )
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)
        
        # Invalid query without LIMIT
        is_valid, errors = validate_query(
            "SELECT * FROM table",
            require_limit=True,
            require_time_range=False
        )
        self.assertFalse(is_valid)
        self.assertEqual(len(errors), 1)
        self.assertIn("LIMIT", errors[0])

    def test_validate_query_limit_and_time_range(self):
        """Test query validation requiring both LIMIT and time range."""
        # Valid query with both
        is_valid, errors = validate_query(
            "SELECT * FROM table WHERE timestamp > '2024-01-01' LIMIT 10",
            require_limit=True,
            require_time_range=True
        )
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)
        
        # Missing LIMIT
        is_valid, errors = validate_query(
            "SELECT * FROM table WHERE timestamp > '2024-01-01'",
            require_limit=True,
            require_time_range=True
        )
        self.assertFalse(is_valid)
        self.assertEqual(len(errors), 1)
        self.assertIn("LIMIT", errors[0])
        
        # Missing time range
        is_valid, errors = validate_query(
            "SELECT * FROM table WHERE id > 100 LIMIT 10",
            require_limit=True,
            require_time_range=True
        )
        self.assertFalse(is_valid)
        self.assertEqual(len(errors), 1)
        self.assertIn("time range", errors[0].lower())
        
        # Missing both
        is_valid, errors = validate_query(
            "SELECT * FROM table WHERE id > 100",
            require_limit=True,
            require_time_range=True
        )
        self.assertFalse(is_valid)
        self.assertEqual(len(errors), 2)

    def test_validate_query_time_range_only(self):
        """Test query validation requiring only time range."""
        # Valid query with time range (no LIMIT required)
        is_valid, errors = validate_query(
            "SELECT * FROM table WHERE timestamp > '2024-01-01'",
            require_limit=False,
            require_time_range=True
        )
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)
        
        # Invalid query without time range
        is_valid, errors = validate_query(
            "SELECT * FROM table WHERE id > 100",
            require_limit=False,
            require_time_range=True
        )
        self.assertFalse(is_valid)
        self.assertEqual(len(errors), 1)

    def test_validate_query_clickhouse_functions(self):
        """Test validation with ClickHouse-specific functions."""
        # Using ClickHouse date functions
        is_valid, errors = validate_query(
            "SELECT * FROM logs WHERE timestamp >= now() - INTERVAL 1 HOUR LIMIT 100",
            require_limit=True,
            require_time_range=True
        )
        self.assertTrue(is_valid)
        
        # Using toDateTime
        is_valid, errors = validate_query(
            "SELECT * FROM logs WHERE created_at >= toDateTime('2024-01-01 00:00:00') LIMIT 50",
            require_limit=True,
            require_time_range=True
        )
        self.assertTrue(is_valid)

    def test_has_limit_with_comments(self):
        """Test LIMIT detection in queries with SQL comments."""
        # Single line comment
        self.assertTrue(has_limit_clause(
            "-- This is a comment\nSELECT * FROM table LIMIT 10"
        ))
        
        # Multi-line comment
        self.assertTrue(has_limit_clause(
            "/* This is a \n multi-line comment */\nSELECT * FROM table LIMIT 10"
        ))

    def test_complex_real_world_queries(self):
        """Test validation on complex real-world-like queries."""
        # Complex analytical query
        query1 = """
        SELECT 
            date_trunc('hour', timestamp) as hour,
            count(*) as event_count,
            uniq(user_id) as unique_users
        FROM events
        WHERE timestamp >= now() - INTERVAL 24 HOUR
          AND event_type = 'page_view'
        GROUP BY hour
        ORDER BY hour DESC
        LIMIT 100
        """
        is_valid, errors = validate_query(query1, require_limit=True, require_time_range=True)
        self.assertTrue(is_valid, f"Errors: {errors}")
        
        # Query with JOIN and time filter
        query2 = """
        SELECT 
            u.user_id,
            u.name,
            count(e.event_id) as event_count
        FROM users u
        JOIN events e ON u.user_id = e.user_id
        WHERE e.timestamp BETWEEN '2024-01-01' AND '2024-01-31'
        GROUP BY u.user_id, u.name
        LIMIT 50
        """
        is_valid, errors = validate_query(query2, require_limit=True, require_time_range=True)
        self.assertTrue(is_valid, f"Errors: {errors}")


if __name__ == "__main__":
    unittest.main()
