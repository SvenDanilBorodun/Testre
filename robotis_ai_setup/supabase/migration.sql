-- ROBOTIS AI Cloud Training - Supabase Schema
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor > New Query)

-- 1. Users table (linked to Supabase Auth)
CREATE TABLE public.users (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  training_credits INTEGER NOT NULL DEFAULT 0,
  trainings_used INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Auto-create user row on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.users (id, email, training_credits)
  VALUES (NEW.id, NEW.email, 0);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- 3. Trainings table
CREATE TABLE public.trainings (
  id SERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES public.users(id),
  status TEXT NOT NULL DEFAULT 'queued',
  dataset_name TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_type TEXT NOT NULL,
  training_params JSONB,
  runpod_job_id TEXT,
  requested_at TIMESTAMPTZ DEFAULT NOW(),
  terminated_at TIMESTAMPTZ,
  error_message TEXT
);

-- 4. Row Level Security
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trainings ENABLE ROW LEVEL SECURITY;

-- Users can only read their own row
CREATE POLICY "Users read own profile"
  ON public.users FOR SELECT
  USING (auth.uid() = id);

-- Users can only read their own trainings
CREATE POLICY "Users read own trainings"
  ON public.trainings FOR SELECT
  USING (auth.uid() = user_id);

-- 5. Indexes for performance
CREATE INDEX idx_trainings_user_id ON public.trainings(user_id);
CREATE INDEX idx_trainings_status ON public.trainings(status);
CREATE INDEX idx_trainings_requested_at ON public.trainings(requested_at DESC);
