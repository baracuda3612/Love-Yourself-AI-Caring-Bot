BEGIN;

DO $$
DECLARE
    has_type boolean;
    has_correct boolean;
    rec record;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'difficulty_level') INTO has_type;
    IF has_type THEN
        SELECT (count(*) = 3 AND bool_and(enumlabel IN ('easy', 'medium', 'hard')))
        INTO has_correct
        FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'difficulty_level';

        IF NOT has_correct THEN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'difficulty_level_tmp') THEN
                CREATE TYPE difficulty_level_tmp AS ENUM ('easy', 'medium', 'hard');
            END IF;

            FOR rec IN
                SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name
                FROM pg_attribute a
                JOIN pg_class c ON a.attrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                JOIN pg_type t ON a.atttypid = t.oid
                WHERE t.typname = 'difficulty_level'
                  AND a.attnum > 0
                  AND NOT a.attisdropped
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I.%I ALTER COLUMN %I TYPE difficulty_level_tmp USING '
                    'CASE '
                    'WHEN %I::text ~ ''^[0-9]+$'' THEN '
                    'CASE WHEN (%I)::int <= 2 THEN ''easy''::difficulty_level_tmp '
                    'WHEN (%I)::int <= 4 THEN ''medium''::difficulty_level_tmp '
                    'ELSE ''hard''::difficulty_level_tmp END '
                    'WHEN %I::text ILIKE ''easy'' THEN ''easy''::difficulty_level_tmp '
                    'WHEN %I::text ILIKE ''medium'' THEN ''medium''::difficulty_level_tmp '
                    'WHEN %I::text ILIKE ''hard'' THEN ''hard''::difficulty_level_tmp '
                    'ELSE %I::text::difficulty_level_tmp END',
                    rec.schema_name,
                    rec.table_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name
                );
            END LOOP;

            DROP TYPE difficulty_level;
            ALTER TYPE difficulty_level_tmp RENAME TO difficulty_level;
        END IF;
    ELSE
        CREATE TYPE difficulty_level AS ENUM ('easy', 'medium', 'hard');
    END IF;
END $$;

DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE udt_name = 'difficultylevel'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.%I ALTER COLUMN %I TYPE difficulty_level USING '
            'CASE '
            'WHEN %I::text ~ ''^[0-9]+$'' THEN '
            'CASE WHEN (%I)::int <= 2 THEN ''easy''::difficulty_level '
            'WHEN (%I)::int <= 4 THEN ''medium''::difficulty_level '
            'ELSE ''hard''::difficulty_level END '
            'WHEN %I::text ILIKE ''easy'' THEN ''easy''::difficulty_level '
            'WHEN %I::text ILIKE ''medium'' THEN ''medium''::difficulty_level '
            'WHEN %I::text ILIKE ''hard'' THEN ''hard''::difficulty_level '
            'ELSE %I::text::difficulty_level END',
            rec.table_schema,
            rec.table_name,
            rec.column_name,
            rec.column_name,
            rec.column_name,
            rec.column_name,
            rec.column_name,
            rec.column_name,
            rec.column_name,
            rec.column_name
        );
    END LOOP;

    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'difficultylevel') THEN
        DROP TYPE difficultylevel;
    END IF;
END $$;

DO $$
DECLARE
    current_type text;
BEGIN
    SELECT udt_name
    INTO current_type
    FROM information_schema.columns
    WHERE table_name = 'ai_plan_steps'
      AND column_name = 'difficulty';

    IF current_type IS NOT NULL AND current_type <> 'difficulty_level' THEN
        ALTER TABLE ai_plan_steps
            ALTER COLUMN difficulty TYPE difficulty_level
            USING CASE
                WHEN difficulty::text ~ '^[0-9]+$' THEN
                    CASE
                        WHEN difficulty::int <= 2 THEN 'easy'::difficulty_level
                        WHEN difficulty::int <= 4 THEN 'medium'::difficulty_level
                        ELSE 'hard'::difficulty_level
                    END
                WHEN difficulty::text ILIKE 'easy' THEN 'easy'::difficulty_level
                WHEN difficulty::text ILIKE 'medium' THEN 'medium'::difficulty_level
                WHEN difficulty::text ILIKE 'hard' THEN 'hard'::difficulty_level
                ELSE difficulty::text::difficulty_level
            END;
    END IF;
END $$;

COMMIT;
