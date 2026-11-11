BEGIN;

CREATE TABLE IF NOT EXISTS max_schedule_journal (
    organization_id text NOT NULL,
    operation_id text NOT NULL,
    post_id text NOT NULL,
    binding jsonb NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('scheduled', 'draft_preserved', 'draft_restored')),
    scheduled_at timestamptz,
    retired_at timestamptz,
    PRIMARY KEY (organization_id, operation_id),
    UNIQUE (organization_id, post_id)
);

CREATE OR REPLACE FUNCTION max_schedule_change(organization text, binding jsonb, action text)
RETURNS jsonb LANGUAGE plpgsql SET lock_timeout = '3s' AS $$
DECLARE
    post_id text := binding->>'post_id';
    operation_id text := binding->>'operation_id';
    draft_due bigint := (binding->>'draft_due_ms')::bigint;
    scheduled_due bigint := (binding->>'due_ms')::bigint;
    deadline bigint := (binding->>'deadline_ms')::bigint;
    post_row "Post"%ROWTYPE;
    prior max_schedule_journal%ROWTYPE;
    post_due bigint;
    result text;
BEGIN
    IF action NOT IN ('schedule', 'retire') OR deadline >= scheduled_due THEN
        RAISE EXCEPTION 'MAX_SCHEDULE_INPUT_INVALID';
    END IF;
    -- The exact native Post row serializes scheduling, retirement and late
    -- state changes by Temporal. No bridge or HTTP calls occur under this lock.
    SELECT p.* INTO post_row
    FROM "Post" p
    JOIN "Integration" i ON i.id = p."integrationId"
    WHERE p.id = post_id AND p."organizationId" = organization
      AND i."organizationId" = organization AND i."providerIdentifier" = 'max-browser'
      AND i.id = binding->>'integration_id'
      AND p."group" = binding->>'group_id' AND p."deletedAt" IS NULL
    FOR UPDATE OF p;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'draft_conflict');
    END IF;

    SELECT j.* INTO prior FROM max_schedule_journal j
    WHERE j.organization_id = organization AND j.post_id = post_row.id;
    IF prior.operation_id IS NOT NULL AND prior.binding IS DISTINCT FROM binding THEN
        RETURN jsonb_build_object('outcome', 'draft_conflict');
    END IF;
    IF prior.outcome IN ('draft_preserved', 'draft_restored') THEN
        RETURN jsonb_build_object('outcome', prior.outcome, 'new_schedule', false);
    END IF;
    IF post_row."parentPostId" IS NOT NULL OR post_row."intervalInDays" IS NOT NULL
        OR nullif(post_row."releaseId", '') IS NOT NULL OR nullif(post_row."releaseURL", '') IS NOT NULL
        OR EXISTS (SELECT 1 FROM "Post" child WHERE child."parentPostId" = post_row.id
                   AND child."deletedAt" IS NULL)
        OR encode(sha256(convert_to(post_row.content, 'UTF8')), 'hex')
            IS DISTINCT FROM binding->>'content_sha256'
        OR encode(sha256(convert_to(post_row.image, 'UTF8')), 'hex')
            IS DISTINCT FROM binding->>'image_json_sha256'
        OR encode(sha256(convert_to(post_row.settings, 'UTF8')), 'hex')
            IS DISTINCT FROM binding->>'settings_sha256' THEN
        RETURN jsonb_build_object('outcome', 'draft_conflict');
    END IF;
    post_due := (extract(epoch FROM post_row."publishDate") * 1000)::bigint;

    IF action = 'schedule' THEN
        IF clock_timestamp() >= to_timestamp(deadline / 1000.0) THEN
            RETURN jsonb_build_object('outcome', 'deadline_elapsed');
        END IF;
        IF prior.outcome = 'scheduled' AND post_row.state = 'QUEUE' AND post_due = scheduled_due THEN
            RETURN jsonb_build_object('outcome', 'scheduled', 'new_schedule', false);
        END IF;
        IF prior.operation_id IS NOT NULL OR post_row.state <> 'DRAFT' OR post_due IS DISTINCT FROM draft_due
            OR NOT EXISTS (SELECT 1 FROM "Integration" i WHERE i.id = post_row."integrationId"
                AND NOT i.disabled AND NOT i."refreshNeeded" AND i."deletedAt" IS NULL) THEN
            RETURN jsonb_build_object('outcome', 'draft_conflict');
        END IF;
        INSERT INTO max_schedule_journal VALUES
            (organization, operation_id, post_row.id, binding, 'scheduled', clock_timestamp(), NULL)
        ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('outcome', 'draft_conflict');
        END IF;
        UPDATE "Post" SET state = 'QUEUE', "publishDate" = timezone('UTC', to_timestamp(scheduled_due / 1000.0)),
            "updatedAt" = timezone('UTC', clock_timestamp())
        WHERE id = post_row.id AND clock_timestamp() < to_timestamp(deadline / 1000.0);
        IF NOT FOUND THEN
            -- Rolls back the journal insert as well as the write.
            RAISE EXCEPTION 'MAX_SCHEDULE_DEADLINE';
        END IF;
        RETURN jsonb_build_object('outcome', 'scheduled', 'new_schedule', true);
    END IF;

    IF clock_timestamp() < to_timestamp(scheduled_due / 1000.0) THEN
        RETURN jsonb_build_object('outcome', 'deadline_pending');
    END IF;
    IF post_row.state = 'DRAFT' AND post_due = draft_due THEN
        result := 'draft_preserved';
    ELSIF prior.outcome = 'scheduled' AND post_row.state IN ('QUEUE', 'ERROR') AND post_due = scheduled_due THEN
        UPDATE "Post" SET state = 'DRAFT', "publishDate" = timezone('UTC', to_timestamp(draft_due / 1000.0)),
            "updatedAt" = timezone('UTC', clock_timestamp())
        WHERE id = post_row.id;
        result := 'draft_restored';
    ELSE
        RETURN jsonb_build_object('outcome', 'draft_conflict');
    END IF;
    IF prior.operation_id IS NULL THEN
        INSERT INTO max_schedule_journal VALUES
            (organization, operation_id, post_row.id, binding, result, NULL, clock_timestamp())
        ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('outcome', 'draft_conflict');
        END IF;
    ELSE
        UPDATE max_schedule_journal j SET outcome = result, retired_at = clock_timestamp()
        WHERE j.organization_id = organization AND j.post_id = post_row.id;
    END IF;
    RETURN jsonb_build_object('outcome', result);
END;
$$;

CREATE OR REPLACE FUNCTION max_schedule_protect_retired() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM max_schedule_journal j
        WHERE j.organization_id = OLD."organizationId" AND j.post_id = OLD.id AND j.retired_at IS NOT NULL) THEN
        RAISE EXCEPTION 'MAX_SCHEDULE_DRAFT_RETIRED';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS max_schedule_retired_draft ON "Post";
CREATE TRIGGER max_schedule_retired_draft BEFORE UPDATE OR DELETE ON "Post"
    FOR EACH ROW EXECUTE FUNCTION max_schedule_protect_retired();

COMMIT;
