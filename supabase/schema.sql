-- Naru Bot — Supabase schema
-- Run this in the SQL editor of your Supabase project.

create table if not exists users (
  id          bigserial primary key,
  chat_id     bigint unique not null,
  name        text,
  email       text,
  phone       text,           -- +91XXXXXXXXXX format
  party_size  int default 2,
  preferences jsonb default '[]'::jsonb,
  is_armed    boolean default false,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

create table if not exists attempts (
  id                bigserial primary key,
  user_id           bigint references users(id) on delete cascade,
  run_id            text,
  success           boolean,
  placed_booking_id bigint,
  razorpay_order_id text,
  slot              jsonb,
  error             text,
  error_code        int,
  created_at        timestamptz default now()
);

create index if not exists idx_users_armed     on users(is_armed) where is_armed = true;
create index if not exists idx_attempts_user   on attempts(user_id, created_at desc);

-- Convenience: bump updated_at on every users row update.
create or replace function bump_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists trg_users_updated on users;
create trigger trg_users_updated before update on users
  for each row execute function bump_updated_at();

-- Row-level security: bot uses service key, no RLS needed.
-- If you ever expose this DB to public, enable RLS here.
