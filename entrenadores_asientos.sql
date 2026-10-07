-- Ejecutar UNA sola vez en Supabase > SQL Editor, y solo si en el panel
-- "Equipo y accesos" > "Asientos de entrenador" sale un error de permisos.
-- Permite que quien ya es entrenador vea, añada y quite otros entrenadores.
-- La funcion public.es_entrenador() ya existe (la usa el panel).

alter table public.entrenadores enable row level security;

drop policy if exists "entrenadores ven la lista" on public.entrenadores;
create policy "entrenadores ven la lista" on public.entrenadores
  for select to authenticated using (public.es_entrenador());

drop policy if exists "entrenadores dan asiento" on public.entrenadores;
create policy "entrenadores dan asiento" on public.entrenadores
  for insert to authenticated with check (public.es_entrenador());

drop policy if exists "entrenadores quitan asiento" on public.entrenadores;
create policy "entrenadores quitan asiento" on public.entrenadores
  for delete to authenticated using (public.es_entrenador());
