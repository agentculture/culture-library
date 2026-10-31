-- Runs once, as the superuser, on an empty data volume (docker-entrypoint-initdb.d).
-- The application password comes from the container environment (a grant secret), never
-- from this file. \getenv needs psql 15+ (the pg17 image has it).
\set ON_ERROR_STOP on
\getenv apppw CULTURE_LIBRARY_PG_PASSWORD
\if :{?apppw}
\else
  \echo 'CULTURE_LIBRARY_PG_PASSWORD is not set: refusing to create the role'
  DO $$ BEGIN RAISE EXCEPTION 'CULTURE_LIBRARY_PG_PASSWORD is not set'; END $$;
\endif

-- Owns the library database but cannot create roles or databases, nor is it a superuser.
CREATE ROLE culture_library LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS PASSWORD :'apppw';

-- C collation: byte-order, locale-independent sorting and comparison.
CREATE DATABASE culture_library OWNER culture_library TEMPLATE template0
  ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';

REVOKE ALL ON DATABASE culture_library FROM PUBLIC;

\connect culture_library
-- Creating an extension needs superuser here, which is why it is done at init, not by the app.
CREATE EXTENSION IF NOT EXISTS vector;
