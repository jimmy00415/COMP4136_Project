CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE rag_releases (
    release_id text PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('loading', 'active', 'failed')),
    expected_movies integer NOT NULL CHECK (expected_movies >= 0),
    expected_assets integer NOT NULL CHECK (expected_assets >= 0),
    expected_metadata_passages integer NOT NULL CHECK (expected_metadata_passages >= 0),
    expected_documents integer NOT NULL CHECK (expected_documents >= 0),
    expected_pdf_passages integer NOT NULL CHECK (expected_pdf_passages >= 0),
    embedding_model text NOT NULL,
    embedding_dimension integer NOT NULL CHECK (embedding_dimension = 768),
    generation_model text NOT NULL,
    access_mode text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz
);

CREATE TABLE ingestion_runs (
    release_id text NOT NULL REFERENCES rag_releases(release_id) ON DELETE RESTRICT,
    run_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    embedded_count integer NOT NULL DEFAULT 0 CHECK (embedded_count >= 0),
    skipped_count integer NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    error_summary text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (release_id, run_id)
);

CREATE TABLE movies (
    release_id text NOT NULL REFERENCES rag_releases(release_id) ON DELETE RESTRICT,
    movie_id text NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL,
    PRIMARY KEY (release_id, movie_id)
);

CREATE TABLE media_assets (
    release_id text NOT NULL,
    asset_id text NOT NULL,
    movie_id text NOT NULL,
    asset_type text NOT NULL,
    content_sha256 text NOT NULL,
    original_object_uri text NOT NULL,
    derived_object_uri text NOT NULL,
    derived_content_sha256 text NOT NULL,
    derived_byte_length bigint,
    derived_mime_type text NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    payload jsonb NOT NULL,
    PRIMARY KEY (release_id, asset_id),
    FOREIGN KEY (release_id, movie_id) REFERENCES movies(release_id, movie_id) ON DELETE RESTRICT,
    CONSTRAINT media_assets_source_sha256_format CHECK (
        content_sha256 = '' OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT media_assets_poster_derivative_contract CHECK (
        asset_type <> 'poster'
        OR (
            is_primary
            AND
            COALESCE(payload ->> 'quality_status', '')
                IN ('machine_passed', 'manual_approved')
            AND content_sha256 ~ '^[0-9a-f]{64}$'
            AND derived_object_uri =
                'assets/posters/derived/' || movie_id || '.webp'
            AND derived_content_sha256 ~ '^[0-9a-f]{64}$'
            AND derived_byte_length IS NOT NULL
            AND derived_byte_length > 0
            AND derived_byte_length <= 16777216
            AND derived_mime_type = 'image/webp'
        )
        OR (
            is_primary
            AND
            COALESCE(payload ->> 'quality_status', '')
                IN ('content_conflict', 'missing', 'placeholder')
            AND (content_sha256 ~ '^[0-9a-f]{64}$'
                OR (COALESCE(payload ->> 'quality_status', '') = 'missing'
                    AND content_sha256 = ''))
            AND derived_object_uri = ''
            AND derived_content_sha256 = ''
            AND derived_byte_length IS NULL
            AND derived_mime_type = ''
        )
    )
);

CREATE UNIQUE INDEX media_assets_one_primary_poster_per_movie
    ON media_assets (release_id, movie_id)
    WHERE is_primary AND asset_type = 'poster';

CREATE TABLE movie_documents (
    release_id text NOT NULL,
    document_id text NOT NULL,
    movie_id text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    source_filename text NOT NULL,
    rights_status text NOT NULL,
    quality_status text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (release_id, document_id),
    UNIQUE (release_id, document_id, movie_id),
    FOREIGN KEY (release_id, movie_id) REFERENCES movies(release_id, movie_id) ON DELETE RESTRICT
);

CREATE TABLE document_chunks (
    release_id text NOT NULL,
    passage_id text NOT NULL,
    movie_id text NOT NULL,
    document_id text,
    passage_kind text NOT NULL CHECK (passage_kind IN ('metadata', 'pdf')),
    page_number integer NOT NULL CHECK (page_number >= 0),
    body text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    embedding vector(768),
    PRIMARY KEY (release_id, passage_id),
    FOREIGN KEY (release_id, movie_id) REFERENCES movies(release_id, movie_id) ON DELETE RESTRICT,
    FOREIGN KEY (release_id, document_id, movie_id)
        REFERENCES movie_documents(release_id, document_id, movie_id) ON DELETE RESTRICT,
    CHECK ((passage_kind = 'metadata' AND document_id IS NULL AND page_number = 0)
        OR (passage_kind = 'pdf' AND document_id IS NOT NULL AND page_number > 0))
);

CREATE INDEX document_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

CREATE FUNCTION reject_payload_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.content_sha256 <> OLD.content_sha256 OR NEW.payload IS DISTINCT FROM OLD.payload THEN
        RAISE EXCEPTION 'content hash and payload are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION reject_movie_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload_sha256 <> OLD.payload_sha256 OR NEW.payload IS DISTINCT FROM OLD.payload THEN
        RAISE EXCEPTION 'movie identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION reject_media_asset_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'media asset identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION reject_chunk_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.content_sha256 <> OLD.content_sha256 OR NEW.body IS DISTINCT FROM OLD.body THEN
        RAISE EXCEPTION 'chunk content is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER movies_immutable
    BEFORE UPDATE ON movies FOR EACH ROW EXECUTE FUNCTION reject_movie_change();

CREATE TRIGGER media_assets_immutable
    BEFORE UPDATE ON media_assets FOR EACH ROW EXECUTE FUNCTION reject_media_asset_change();

CREATE TRIGGER movie_documents_immutable
    BEFORE UPDATE ON movie_documents FOR EACH ROW EXECUTE FUNCTION reject_payload_change();

CREATE TRIGGER document_chunks_immutable
    BEFORE UPDATE ON document_chunks FOR EACH ROW EXECUTE FUNCTION reject_chunk_change();
