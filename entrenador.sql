-- Ejecutar UNA vez en Supabase > SQL Editor (despues de jugadas.sql).
-- Crea lo necesario para la app del Entrenador: carpetas, playbooks, ejercicios,
-- planes de entrenamiento y archivos (videos, imagenes, audios).

-- ---------- Carpetas (para jugadas, ejercicios y planes) ----------
create table if not exists public.carpetas (
  tipo text not null,           -- 'jugadas' | 'ejercicios' | 'planes'
  nombre text not null,
  creada timestamptz not null default now(),
  primary key (tipo, nombre)
);

-- ---------- Playbooks: colecciones de jugadas ----------
create table if not exists public.playbooks (
  id text primary key,
  nombre text not null default '',
  equipo text,
  temporada text,
  compartido boolean not null default false,
  data jsonb not null default '{}'::jsonb,   -- { jugadas: [ids en orden], notas }
  actualizada timestamptz not null default now()
);

-- ---------- Ejercicios ----------
create table if not exists public.ejercicios (
  id text primary key,
  nombre text not null default '',
  carpeta text,
  data jsonb not null default '{}'::jsonb,   -- { categoria, minutos, descripcion, materiales, video, jugadaId }
  actualizada timestamptz not null default now()
);

-- ---------- Planes de entrenamiento ----------
create table if not exists public.planes (
  id text primary key,
  nombre text not null default '',
  carpeta text,
  fecha date,
  data jsonb not null default '{}'::jsonb,   -- { ejercicios: [{id, min, notas}], notas }
  actualizada timestamptz not null default now()
);

-- ---------- Archivos subidos ----------
create table if not exists public.archivos (
  id text primary key,
  nombre text not null default '',
  tipo text not null,            -- 'video' | 'imagen' | 'audio'
  ruta text not null,            -- ruta dentro del bucket 'entrenador'
  mime text,
  tamano bigint,
  creada timestamptz not null default now()
);

-- ---------- Permisos: solo entrenadores (los playbooks compartidos los ven todas) ----------
do $$
declare t text;
begin
  foreach t in array array['carpetas','playbooks','ejercicios','planes','archivos'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists "%s ver" on public.%I', t, t);
    execute format('drop policy if exists "%s crear" on public.%I', t, t);
    execute format('drop policy if exists "%s cambiar" on public.%I', t, t);
    execute format('drop policy if exists "%s borrar" on public.%I', t, t);
    if t = 'playbooks' then
      execute 'create policy "playbooks ver" on public.playbooks for select to authenticated using (public.es_entrenador() or compartido = true)';
    else
      execute format('create policy "%s ver" on public.%I for select to authenticated using (public.es_entrenador())', t, t);
    end if;
    execute format('create policy "%s crear" on public.%I for insert to authenticated with check (public.es_entrenador())', t, t);
    execute format('create policy "%s cambiar" on public.%I for update to authenticated using (public.es_entrenador()) with check (public.es_entrenador())', t, t);
    execute format('create policy "%s borrar" on public.%I for delete to authenticated using (public.es_entrenador())', t, t);
    -- Si ya usas roles_entrenadores.sql: solo el propietario escribe
    if to_regprocedure('public.puede_escribir()') is not null then
      execute format('drop policy if exists solo_propietario_insert on public.%I', t);
      execute format('drop policy if exists solo_propietario_update on public.%I', t);
      execute format('drop policy if exists solo_propietario_delete on public.%I', t);
      execute format('create policy solo_propietario_insert on public.%I as restrictive for insert to authenticated with check (public.puede_escribir())', t);
      execute format('create policy solo_propietario_update on public.%I as restrictive for update to authenticated using (public.puede_escribir()) with check (public.puede_escribir())', t);
      execute format('create policy solo_propietario_delete on public.%I as restrictive for delete to authenticated using (public.puede_escribir())', t);
    end if;
  end loop;
end $$;

-- ---------- Almacen de archivos (bucket publico de lectura; solo entrenadores suben o borran) ----------
insert into storage.buckets (id, name, public)
values ('entrenador', 'entrenador', true)
on conflict (id) do nothing;

drop policy if exists "entrenador sube archivos" on storage.objects;
create policy "entrenador sube archivos" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'entrenador' and public.es_entrenador());

drop policy if exists "entrenador borra archivos" on storage.objects;
create policy "entrenador borra archivos" on storage.objects
  for delete to authenticated
  using (bucket_id = 'entrenador' and public.es_entrenador());
