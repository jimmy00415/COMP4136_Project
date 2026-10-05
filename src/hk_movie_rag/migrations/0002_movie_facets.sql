CREATE TABLE movie_facets (
    release_id text NOT NULL,
    movie_id text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    tier text NOT NULL CHECK (tier IN ('S', 'A', 'B')),
    tier_reason text NOT NULL CHECK (tier_reason <> ''),
    human_review text NOT NULL CHECK (human_review <> ''),
    pilot_movie boolean NOT NULL,
    pilot_evidence jsonb,
    PRIMARY KEY (release_id, movie_id),
    FOREIGN KEY (release_id, movie_id) REFERENCES movies(release_id, movie_id)
        ON DELETE RESTRICT,
    CHECK ((pilot_movie AND pilot_evidence IS NOT NULL)
        OR (NOT pilot_movie AND pilot_evidence IS NULL))
);

CREATE INDEX movie_facets_release_tier ON movie_facets (release_id, tier, movie_id);
CREATE INDEX movie_facets_release_pilot ON movie_facets (release_id, pilot_movie, movie_id);

CREATE FUNCTION reject_movie_facet_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'movie facet identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER movie_facets_immutable
    BEFORE UPDATE OR DELETE ON movie_facets
    FOR EACH ROW EXECUTE FUNCTION reject_movie_facet_change();
