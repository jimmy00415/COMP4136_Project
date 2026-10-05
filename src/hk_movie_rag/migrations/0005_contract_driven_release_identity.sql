ALTER TABLE rag_releases
    ADD COLUMN IF NOT EXISTS manifest_sha256 text,
    ADD COLUMN IF NOT EXISTS document_embedding_profile text,
    ADD COLUMN IF NOT EXISTS contract_json jsonb;

CREATE OR REPLACE FUNCTION public.rag_release_manifest_identity_v4_is_valid(
    release_id_value text,
    expected_movies_value integer,
    expected_assets_value integer,
    expected_metadata_passages_value integer,
    expected_documents_value integer,
    expected_pdf_passages_value integer,
    embedding_model_value text,
    embedding_dimension_value integer,
    generation_model_value text,
    access_mode_value text,
    manifest_sha256_value text,
    document_embedding_profile_value text,
    contract_json_value jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $function$
SELECT (
    (
        manifest_sha256_value IS NULL
        AND document_embedding_profile_value IS NULL
        AND contract_json_value IS NULL
    ) OR (
        manifest_sha256_value IS NOT NULL
        AND (manifest_sha256_value ~ '^[0-9a-f]{64}$') IS TRUE
        AND document_embedding_profile_value IS NOT NULL
        AND btrim(document_embedding_profile_value) <> ''
        AND (
            contract_json_value IS NULL
            OR CASE
                WHEN jsonb_typeof(contract_json_value) IS DISTINCT FROM 'object'
                    THEN FALSE
                WHEN NOT contract_json_value ?& ARRAY[
                    'schema_version',
                    'rag_release_id',
                    'parent_release_manifest_sha256',
                    'manifest_sha256',
                    'bundle_sha256',
                    'derived_inventory_sha256',
                    'counts',
                    'embedding_model',
                    'embedding_dimension',
                    'generation_model',
                    'text_extraction_profile',
                    'document_embedding_profile',
                    'relevance_policy_sha256',
                    'poster_authority_sha256',
                    'access_mode'
                ]::text[] THEN FALSE
                WHEN (
                    SELECT count(*)
                    FROM jsonb_object_keys(contract_json_value)
                ) <> 15 THEN FALSE
                WHEN jsonb_typeof(contract_json_value -> 'counts')
                     IS DISTINCT FROM 'object' THEN FALSE
                WHEN NOT (contract_json_value -> 'counts') ?& ARRAY[
                    'movies',
                    'facet_count',
                    'tier_s_count',
                    'tier_a_count',
                    'tier_b_count',
                    'pilot_count',
                    'poster_rows',
                    'primary_poster_rows',
                    'approved_poster_objects',
                    'unavailable_poster_rows',
                    'derived_poster_bytes',
                    'metadata_passages',
                    'documents',
                    'pdf_passages'
                ]::text[] THEN FALSE
                WHEN (
                    SELECT count(*)
                    FROM jsonb_object_keys(contract_json_value -> 'counts')
                ) <> 14 THEN FALSE
                WHEN EXISTS (
                    SELECT 1
                    FROM unnest(ARRAY[
                        'schema_version',
                        'rag_release_id',
                        'parent_release_manifest_sha256',
                        'manifest_sha256',
                        'bundle_sha256',
                        'derived_inventory_sha256',
                        'embedding_model',
                        'generation_model',
                        'text_extraction_profile',
                        'document_embedding_profile',
                        'access_mode'
                    ]::text[]) AS required_text(key_name)
                    WHERE jsonb_typeof(contract_json_value -> required_text.key_name)
                              IS DISTINCT FROM 'string'
                       OR btrim(contract_json_value ->> required_text.key_name) = ''
                ) THEN FALSE
                WHEN EXISTS (
                    SELECT 1
                    FROM unnest(ARRAY[
                        'movies',
                        'facet_count',
                        'tier_s_count',
                        'tier_a_count',
                        'tier_b_count',
                        'pilot_count',
                        'poster_rows',
                        'primary_poster_rows',
                        'approved_poster_objects',
                        'unavailable_poster_rows',
                        'derived_poster_bytes',
                        'metadata_passages',
                        'documents',
                        'pdf_passages'
                    ]::text[]) AS required_count(key_name)
                    WHERE jsonb_typeof(
                              contract_json_value -> 'counts' -> required_count.key_name
                          ) IS DISTINCT FROM 'number'
                       OR (
                              contract_json_value -> 'counts' ->> required_count.key_name
                          ~ '^(0|[1-9][0-9]*)$') IS NOT TRUE
                ) THEN FALSE
                WHEN jsonb_typeof(contract_json_value -> 'embedding_dimension')
                     IS DISTINCT FROM 'number' THEN FALSE
                WHEN (
                    contract_json_value ->> 'embedding_dimension'
                    ~ '^(0|[1-9][0-9]*)$'
                ) IS NOT TRUE THEN FALSE
                WHEN (
                    (contract_json_value ->> 'parent_release_manifest_sha256'
                     ~ '^[0-9a-f]{64}$') IS NOT TRUE
                    OR (contract_json_value ->> 'manifest_sha256'
                        ~ '^[0-9a-f]{64}$') IS NOT TRUE
                    OR (contract_json_value ->> 'bundle_sha256'
                        ~ '^[0-9a-f]{64}$') IS NOT TRUE
                    OR (contract_json_value ->> 'derived_inventory_sha256'
                        ~ '^[0-9a-f]{64}$') IS NOT TRUE
                ) THEN FALSE
                WHEN NOT (
                    jsonb_typeof(contract_json_value -> 'relevance_policy_sha256')
                    = 'null'
                    OR (
                        jsonb_typeof(contract_json_value -> 'relevance_policy_sha256')
                        = 'string'
                        AND (contract_json_value ->> 'relevance_policy_sha256'
                             ~ '^[0-9a-f]{64}$') IS TRUE
                    )
                ) THEN FALSE
                WHEN NOT (
                    jsonb_typeof(contract_json_value -> 'poster_authority_sha256')
                    = 'null'
                    OR (
                        jsonb_typeof(contract_json_value -> 'poster_authority_sha256')
                        = 'string'
                        AND (contract_json_value ->> 'poster_authority_sha256'
                             ~ '^[0-9a-f]{64}$') IS TRUE
                    )
                ) THEN FALSE
                ELSE
                    contract_json_value ->> 'rag_release_id' = release_id_value
                    AND contract_json_value ->> 'manifest_sha256'
                        = manifest_sha256_value
                    AND contract_json_value ->> 'document_embedding_profile'
                        = document_embedding_profile_value
                    AND contract_json_value ->> 'embedding_model'
                        = embedding_model_value
                    AND (contract_json_value ->> 'embedding_dimension')::numeric
                        = embedding_dimension_value
                    AND (contract_json_value ->> 'embedding_dimension')::numeric = 768
                    AND contract_json_value ->> 'generation_model'
                        = generation_model_value
                    AND contract_json_value ->> 'access_mode' = access_mode_value
                    AND (contract_json_value -> 'counts' ->> 'movies')::numeric
                        = expected_movies_value
                    AND (contract_json_value -> 'counts' ->> 'poster_rows')::numeric
                        = expected_assets_value
                    AND (
                        contract_json_value -> 'counts' ->> 'metadata_passages'
                    )::numeric = expected_metadata_passages_value
                    AND (contract_json_value -> 'counts' ->> 'documents')::numeric
                        = expected_documents_value
                    AND (contract_json_value -> 'counts' ->> 'pdf_passages')::numeric
                        = expected_pdf_passages_value
                    AND (contract_json_value -> 'counts' ->> 'facet_count')::numeric
                        = (contract_json_value -> 'counts' ->> 'movies')::numeric
                    AND (
                        (contract_json_value -> 'counts' ->> 'tier_s_count')::numeric
                        + (contract_json_value -> 'counts' ->> 'tier_a_count')::numeric
                        + (contract_json_value -> 'counts' ->> 'tier_b_count')::numeric
                    ) = (contract_json_value -> 'counts' ->> 'facet_count')::numeric
                    AND (contract_json_value -> 'counts' ->> 'pilot_count')::numeric
                        <= (contract_json_value -> 'counts' ->> 'facet_count')::numeric
                    AND (
                        contract_json_value -> 'counts' ->> 'primary_poster_rows'
                    )::numeric = (
                        contract_json_value -> 'counts' ->> 'poster_rows'
                    )::numeric
                    AND (
                        (contract_json_value -> 'counts'
                         ->> 'approved_poster_objects')::numeric
                        + (contract_json_value -> 'counts'
                           ->> 'unavailable_poster_rows')::numeric
                    ) = (
                        contract_json_value -> 'counts' ->> 'primary_poster_rows'
                    )::numeric
            END
        )
    )
) IS TRUE;
$function$;

ALTER TABLE rag_releases
    DROP CONSTRAINT IF EXISTS rag_releases_manifest_identity_format;

ALTER TABLE rag_releases
    ADD CONSTRAINT rag_releases_manifest_identity_format CHECK (
        public.rag_release_manifest_identity_v4_is_valid(
            release_id,
            expected_movies,
            expected_assets,
            expected_metadata_passages,
            expected_documents,
            expected_pdf_passages,
            embedding_model,
            embedding_dimension,
            generation_model,
            access_mode,
            manifest_sha256,
            document_embedding_profile,
            contract_json
        ) IS TRUE
    );

CREATE OR REPLACE FUNCTION public.enforce_release_manifest_identity()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF public.rag_release_manifest_identity_v4_is_valid(
            NEW.release_id,
            NEW.expected_movies,
            NEW.expected_assets,
            NEW.expected_metadata_passages,
            NEW.expected_documents,
            NEW.expected_pdf_passages,
            NEW.embedding_model,
            NEW.embedding_dimension,
            NEW.generation_model,
            NEW.access_mode,
            NEW.manifest_sha256,
            NEW.document_embedding_profile,
            NEW.contract_json
        ) IS NOT TRUE OR NEW.contract_json IS NULL THEN
            RAISE EXCEPTION 'new release requires verified manifest identity';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.expected_movies IS DISTINCT FROM OLD.expected_movies
       OR NEW.expected_assets IS DISTINCT FROM OLD.expected_assets
       OR NEW.expected_metadata_passages IS DISTINCT FROM OLD.expected_metadata_passages
       OR NEW.expected_documents IS DISTINCT FROM OLD.expected_documents
       OR NEW.expected_pdf_passages IS DISTINCT FROM OLD.expected_pdf_passages
       OR NEW.embedding_model IS DISTINCT FROM OLD.embedding_model
       OR NEW.embedding_dimension IS DISTINCT FROM OLD.embedding_dimension
       OR NEW.generation_model IS DISTINCT FROM OLD.generation_model
       OR NEW.access_mode IS DISTINCT FROM OLD.access_mode THEN
        RAISE EXCEPTION 'release contract is immutable';
    END IF;

    IF OLD.manifest_sha256 IS NULL
       AND OLD.document_embedding_profile IS NULL
       AND OLD.contract_json IS NULL THEN
        IF public.rag_release_manifest_identity_v4_is_valid(
            NEW.release_id,
            NEW.expected_movies,
            NEW.expected_assets,
            NEW.expected_metadata_passages,
            NEW.expected_documents,
            NEW.expected_pdf_passages,
            NEW.embedding_model,
            NEW.embedding_dimension,
            NEW.generation_model,
            NEW.access_mode,
            NEW.manifest_sha256,
            NEW.document_embedding_profile,
            NEW.contract_json
        ) IS NOT TRUE OR NEW.contract_json IS NULL THEN
            RAISE EXCEPTION 'legacy release identity claim must be complete';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.manifest_sha256 IS NOT NULL
       AND OLD.document_embedding_profile IS NOT NULL
       AND OLD.contract_json IS NULL
       AND NEW.manifest_sha256 IS NOT DISTINCT FROM OLD.manifest_sha256
       AND NEW.document_embedding_profile IS NOT DISTINCT FROM OLD.document_embedding_profile
       AND NEW.contract_json IS NOT NULL
       AND public.rag_release_manifest_identity_v4_is_valid(
           NEW.release_id,
           NEW.expected_movies,
           NEW.expected_assets,
           NEW.expected_metadata_passages,
           NEW.expected_documents,
           NEW.expected_pdf_passages,
           NEW.embedding_model,
           NEW.embedding_dimension,
           NEW.generation_model,
           NEW.access_mode,
           NEW.manifest_sha256,
           NEW.document_embedding_profile,
           NEW.contract_json
       ) IS TRUE THEN
        RETURN NEW;
    END IF;

    IF NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
       OR NEW.document_embedding_profile IS DISTINCT FROM OLD.document_embedding_profile
       OR NEW.contract_json IS DISTINCT FROM OLD.contract_json THEN
        RAISE EXCEPTION 'release manifest identity is immutable';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS rag_releases_manifest_identity_immutable ON rag_releases;

CREATE TRIGGER rag_releases_manifest_identity_immutable
    BEFORE INSERT OR UPDATE ON rag_releases
    FOR EACH ROW EXECUTE FUNCTION public.enforce_release_manifest_identity();
