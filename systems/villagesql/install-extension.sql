-- The extension declares two preview capabilities, so the server must allow preview extensions
-- (SET PERSIST survives a restart of the container on the same data volume).
SET PERSIST vsql_allow_preview_extensions = ON;
INSTALL EXTENSION vsql_duckdb;
