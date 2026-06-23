-- 004_delegate.sql
-- Adds the optional delegate authority pointer used by the gap scheduler to
-- select the correct foreign-search job type for delegated countries.
ALTER TABLE countries
  ADD COLUMN delegate_iso2 TEXT
    REFERENCES countries(iso2);
