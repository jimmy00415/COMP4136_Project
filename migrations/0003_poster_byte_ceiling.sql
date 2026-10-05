DO $migration$
BEGIN
    IF to_regclass('public.media_assets') IS NULL THEN
        RAISE EXCEPTION 'media_assets must exist before poster byte ceiling migration';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = to_regclass('public.media_assets')
          AND conname = 'media_assets_poster_byte_ceiling'
          AND contype = 'c'
    ) THEN
        ALTER TABLE media_assets
            ADD CONSTRAINT media_assets_poster_byte_ceiling CHECK (
                asset_type <> 'poster'
                OR derived_byte_length IS NULL
                OR derived_byte_length <= 16777216
            );
    END IF;
END
$migration$;
