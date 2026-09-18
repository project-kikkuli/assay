-- Rolling-schema phases. Matrix sections recreate the table (compat probe);
-- the true rollout path (verifier scenario_rollout) uses ALTER on live rows.
-- PHASE comments are parsed by verifier.py; do not rename them.

-- PHASE: original
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL
);

-- PHASE: naive_rename
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name TEXT NOT NULL
);

-- PHASE: expand
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  display_name TEXT NULL
);

-- PHASE: bridge
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NULL,
  display_name TEXT NULL,
  CONSTRAINT assay_names_equal CHECK
    (title IS NOT NULL AND display_name IS NOT NULL AND title = display_name)
);

-- TRIGGER: bridge_sync
-- Single-field change propagates to the other field; both-changed to
-- conflicting values rejects; NULL result rejects. A trigger CANNOT tell a
-- legitimate one-field client write from an unguarded stale backfill with
-- identical SQL: both are accepted and both fields converge (see verifier).
CREATE OR REPLACE FUNCTION assay_sync_title_display() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.title IS NULL AND NEW.display_name IS NULL THEN
      RAISE EXCEPTION 'assay: both null' USING ERRCODE = '23502';
    ELSIF NEW.title IS NULL THEN
      NEW.title := NEW.display_name;
    ELSIF NEW.display_name IS NULL THEN
      NEW.display_name := NEW.title;
    ELSIF NEW.title <> NEW.display_name THEN
      RAISE EXCEPTION 'assay: conflicting insert' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.title IS DISTINCT FROM OLD.title
     AND NEW.display_name IS DISTINCT FROM OLD.display_name THEN
    IF NEW.title IS NULL OR NEW.display_name IS NULL THEN
      RAISE EXCEPTION 'assay: null update' USING ERRCODE = '23502';
    ELSIF NEW.title <> NEW.display_name THEN
      RAISE EXCEPTION 'assay: conflicting update' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
  ELSIF NEW.title IS DISTINCT FROM OLD.title THEN
    NEW.display_name := NEW.title;
  ELSIF NEW.display_name IS DISTINCT FROM OLD.display_name THEN
    NEW.title := NEW.display_name;
  ELSE
    RETURN NEW;
  END IF;
  IF NEW.title IS NULL OR NEW.display_name IS NULL THEN
    RAISE EXCEPTION 'assay: null propagated' USING ERRCODE = '23502';
  ELSIF NEW.title <> NEW.display_name THEN
    RAISE EXCEPTION 'assay: invariant' USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS assay_sync_title_display ON documents;
CREATE TRIGGER assay_sync_title_display
  BEFORE INSERT OR UPDATE ON documents
  FOR EACH ROW EXECUTE FUNCTION assay_sync_title_display();

-- PHASE: contract
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name TEXT NOT NULL
);
