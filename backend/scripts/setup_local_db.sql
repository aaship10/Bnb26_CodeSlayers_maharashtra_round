-- One-time setup for a natively installed Postgres.
-- Run as the postgres superuser, e.g.:
--   "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -h localhost -p 5432 -f scripts/setup_local_db.sql
-- Safe to re-run: existing role/databases are left alone.

SELECT 'CREATE ROLE fairdrop LOGIN CREATEDB PASSWORD ''fairdrop'''
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fairdrop')
\gexec

SELECT 'CREATE DATABASE fairdrop OWNER fairdrop'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'fairdrop')
\gexec

SELECT 'CREATE DATABASE fairdrop_test OWNER fairdrop'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'fairdrop_test')
\gexec
