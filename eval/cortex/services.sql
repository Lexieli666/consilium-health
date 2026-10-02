-- Two Cortex Search services over the CHUNKS table written by `eval.cortex.load_corpus`: one per
-- embedding model, so the model is the only variable between them.  Run as a role holding
-- CREATE CORTEX SEARCH SERVICE on CONSILIUM.EVAL, SELECT on CHUNKS and USAGE on CONSILIUM_WH
-- (ACCOUNTADMIN on a throwaway trial; a dedicated role in a real deployment).
--
-- TARGET_LAG is a day on purpose: the corpus is static, and a short lag would spend warehouse
-- credits refreshing an index that never changes.

USE ROLE ACCOUNTADMIN;
CREATE WAREHOUSE IF NOT EXISTS CONSILIUM_WH WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE;
CREATE DATABASE IF NOT EXISTS CONSILIUM;
CREATE SCHEMA IF NOT EXISTS CONSILIUM.EVAL;
USE CONSILIUM.EVAL;
USE WAREHOUSE CONSILIUM_WH;

-- Sanity checks on the loaded table (chunks are 800-1,000 characters, under the 512-token guidance
-- for the search column, so no re-chunking is needed).
SELECT CATEGORY, COUNT(*) FROM CHUNKS GROUP BY 1 ORDER BY 1;
SELECT COUNT(*) AS n_chunks, COUNT(DISTINCT DOC_ID) AS n_docs, MAX(LENGTH(CHUNK_TEXT)) AS longest FROM CHUNKS;

CREATE OR REPLACE CORTEX SEARCH SERVICE CONSILIUM_M15
  ON CHUNK_TEXT
  ATTRIBUTES CATEGORY, DOC_ID
  WAREHOUSE = CONSILIUM_WH
  TARGET_LAG = '1 day'
  EMBEDDING_MODEL = 'snowflake-arctic-embed-m-v1.5'
AS SELECT CHUNK_ID, DOC_ID, CHUNK_INDEX, CATEGORY, TITLE, CHUNK_TEXT FROM CHUNKS;

CREATE OR REPLACE CORTEX SEARCH SERVICE CONSILIUM_L20
  ON CHUNK_TEXT
  ATTRIBUTES CATEGORY, DOC_ID
  WAREHOUSE = CONSILIUM_WH
  TARGET_LAG = '1 day'
  EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0'
AS SELECT CHUNK_ID, DOC_ID, CHUNK_INDEX, CATEGORY, TITLE, CHUNK_TEXT FROM CHUNKS;

SHOW CORTEX SEARCH SERVICES;             -- wait until both are indexed before running the eval
DESCRIBE CORTEX SEARCH SERVICE CONSILIUM_M15;

-- Smoke test before any Python runs.
SELECT PARSE_JSON(SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
  'CONSILIUM.EVAL.CONSILIUM_M15',
  '{"query": "what test confirms a diagnosis of COPD", "columns": ["DOC_ID", "CHUNK_ID"], "limit": 5}'
))['results'];

-- Cost, after the runs (ACCOUNT_USAGE lags by up to a few hours).
SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_SEARCH_SERVING_USAGE_HISTORY ORDER BY START_TIME DESC LIMIT 20;
SELECT SERVICE_TYPE, SUM(CREDITS_USED) FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY
 WHERE START_TIME > DATEADD(day, -3, CURRENT_TIMESTAMP()) GROUP BY 1;
