/*
  Dedicated read-only login for the eds-rag MCP tools.

  The tools refuse to start if this login can write (see
  eds_rag/executor.py: PERMISSION_CHECK_SQL), so this script is the
  permission model, not a suggestion. Run as a sysadmin, and run it in a
  lower environment first.

  Azure SQL Database: skip the master section and create a contained user
  instead:  CREATE USER eds_rag_reader WITH PASSWORD = '...';
*/

-- 1. Server login --------------------------------------------------------
USE master;
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'eds_rag_reader')
    CREATE LOGIN eds_rag_reader
        WITH PASSWORD = '<generate a strong password>',   -- store in a secret manager
             CHECK_POLICY = ON,
             DEFAULT_DATABASE = EDS;
GO

-- 2. Database user: read + showplan, nothing else ------------------------
USE EDS;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'eds_rag_reader')
    CREATE USER eds_rag_reader FOR LOGIN eds_rag_reader;
GO
ALTER ROLE db_datareader ADD MEMBER eds_rag_reader;

-- explain_query needs SHOWPLAN; schema introspection needs metadata visibility.
GRANT SHOWPLAN TO eds_rag_reader;
GRANT VIEW DEFINITION TO eds_rag_reader;

-- Belt and braces: explicit DENY wins over any role membership added later.
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE, CREATE TABLE, CREATE VIEW,
     CREATE PROCEDURE, CREATE FUNCTION, REFERENCES, TAKE OWNERSHIP
  TO eds_rag_reader;
GO

-- 3. Verify (every column should be 0) --------------------------------------
EXECUTE AS LOGIN = 'eds_rag_reader';
SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT')       AS can_insert,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE')       AS can_update,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE')       AS can_delete,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER')        AS can_alter,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'EXECUTE')      AS can_execute,
       IS_ROLEMEMBER('db_datawriter')                           AS is_datawriter,
       IS_ROLEMEMBER('db_owner')                                AS is_owner;
REVERT;
GO
