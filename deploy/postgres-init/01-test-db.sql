-- Runs once, on first start of the data volume: a separate DB for the
-- test suite (every test module refuses to run unless the DB name
-- contains 'test' — feedback_destructive_test_fixture).
CREATE DATABASE customerintel_test OWNER customerintel;
