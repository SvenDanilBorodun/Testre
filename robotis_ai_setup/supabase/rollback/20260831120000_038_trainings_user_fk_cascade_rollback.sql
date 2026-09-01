-- Rollback for 038 — restores the NO ACTION foreign key on
-- public.trainings.user_id (the baseline.sql:62 spelling).
--
-- WARNING, read before running: with this applied, deleting a student who has
-- any `trainings` row raises 23503 again, i.e. `DELETE
-- /teacher/students/{id}` answers 500 and a GDPR Art. 17 erasure cannot be
-- completed for that student. Only roll this back if the CASCADE itself is
-- causing harm (e.g. training rows disappearing that some future audit
-- surface depends on), and expect the deletion route to fail meanwhile.
--
-- No data is restored by this script: rows already cascaded away are gone.

ALTER TABLE public.trainings
  DROP CONSTRAINT trainings_user_id_fkey;

ALTER TABLE public.trainings
  ADD CONSTRAINT trainings_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES public.users(id);
