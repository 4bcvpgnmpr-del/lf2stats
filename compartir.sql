-- Ejecutar UNA vez en Supabase > SQL Editor.
-- Enlaces para compartir (solo lectura) una jugada, ejercicio o plan.
-- Quien tenga el enlace lo ve sin cuenta; no puede listar ni editar nada.

create table if not exists public.compartidos (
  token text primary key,
  tipo text not null,            -- 'jugada' | 'ejercicio' | 'plan'
  nombre text not null default '',
  data jsonb not null,
  creada timestamptz not null default now()
);
alter table public.compartidos enable row level security;

drop policy if exists "compartidos ver" on public.compartidos;
drop policy if exists "compartidos crear" on public.compartidos;
drop policy if exists "compartidos cambiar" on public.compartidos;
drop policy if exists "compartidos borrar" on public.compartidos;
create policy "compartidos ver" on public.compartidos for select to authenticated using (public.es_entrenador());
create policy "compartidos crear" on public.compartidos for insert to authenticated with check (public.es_entrenador());
create policy "compartidos cambiar" on public.compartidos for update to authenticated using (public.es_entrenador()) with check (public.es_entrenador());
create policy "compartidos borrar" on public.compartidos for delete to authenticated using (public.es_entrenador());

-- Si usas roles_entrenadores.sql: solo el propietario escribe
do $$
begin
  if to_regprocedure('public.puede_escribir()') is not null then
    drop policy if exists solo_propietario_insert on public.compartidos;
    drop policy if exists solo_propietario_update on public.compartidos;
    drop policy if exists solo_propietario_delete on public.compartidos;
    create policy solo_propietario_insert on public.compartidos as restrictive for insert to authenticated with check (public.puede_escribir());
    create policy solo_propietario_update on public.compartidos as restrictive for update to authenticated using (public.puede_escribir()) with check (public.puede_escribir());
    create policy solo_propietario_delete on public.compartidos as restrictive for delete to authenticated using (public.puede_escribir());
  end if;
end $$;

-- El visitante solo puede pedir UN elemento si conoce su token (no hay forma de listarlos)
create or replace function public.obtener_compartido(p_token text)
returns table(tipo text, nombre text, data jsonb)
language sql stable security definer set search_path = public as $$
  select c.tipo, c.nombre, c.data from public.compartidos c where c.token = p_token limit 1;
$$;
revoke all on function public.obtener_compartido(text) from public;
grant execute on function public.obtener_compartido(text) to anon, authenticated;
