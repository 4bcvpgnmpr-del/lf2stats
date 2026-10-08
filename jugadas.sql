-- Ejecutar UNA vez en Supabase > SQL Editor.
-- Guarda tus jugadas de la pizarra en la nube. Solo los entrenadores las ven y editan;
-- las jugadoras solo ven las que marques como "Visible para las jugadoras".

create table if not exists public.jugadas (
  id text primary key,
  nombre text not null default '',
  tipo text,
  carpeta text,
  favorita boolean not null default false,
  compartida boolean not null default false,
  data jsonb not null,
  actualizada timestamptz not null default now()
);

alter table public.jugadas enable row level security;

drop policy if exists "jugadas ver" on public.jugadas;
create policy "jugadas ver" on public.jugadas for select to authenticated
  using (public.es_entrenador() or compartida = true);

drop policy if exists "jugadas crear" on public.jugadas;
create policy "jugadas crear" on public.jugadas for insert to authenticated
  with check (public.es_entrenador());

drop policy if exists "jugadas cambiar" on public.jugadas;
create policy "jugadas cambiar" on public.jugadas for update to authenticated
  using (public.es_entrenador()) with check (public.es_entrenador());

drop policy if exists "jugadas borrar" on public.jugadas;
create policy "jugadas borrar" on public.jugadas for delete to authenticated
  using (public.es_entrenador());

-- Si ya ejecutaste roles_entrenadores.sql, los demas entrenadores no pueden escribir
do $$
begin
  if to_regprocedure('public.puede_escribir()') is not null then
    execute 'drop policy if exists solo_propietario_insert on public.jugadas';
    execute 'drop policy if exists solo_propietario_update on public.jugadas';
    execute 'drop policy if exists solo_propietario_delete on public.jugadas';
    execute 'create policy solo_propietario_insert on public.jugadas as restrictive for insert to authenticated with check (public.puede_escribir())';
    execute 'create policy solo_propietario_update on public.jugadas as restrictive for update to authenticated using (public.puede_escribir()) with check (public.puede_escribir())';
    execute 'create policy solo_propietario_delete on public.jugadas as restrictive for delete to authenticated using (public.puede_escribir())';
  end if;
end $$;
