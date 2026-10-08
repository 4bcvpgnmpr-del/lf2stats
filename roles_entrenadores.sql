-- Ejecutar UNA vez en Supabase > SQL Editor.
-- Protege de verdad que solo el propietario (guardado en ajustes, clave "propietario")
-- pueda escribir. Los demas entrenadores solo leen. Las jugadoras no se ven afectadas.

create or replace function public.es_propietario() returns boolean
language sql stable security definer set search_path = public as $$
  select coalesce(
    (select lower(valor) = lower(coalesce(auth.jwt()->>'email','')) from public.ajustes where clave = 'propietario' limit 1),
    true);
$$;

create or replace function public.puede_escribir() returns boolean
language sql stable security definer set search_path = public as $$
  select not public.es_entrenador() or public.es_propietario();
$$;

do $$
declare t text; c text;
begin
  foreach t in array array['jugadoras','informes','ajustes','entrenadores'] loop
    if to_regclass('public.'||t) is not null then
      foreach c in array array['insert','update','delete'] loop
        execute format('drop policy if exists %I on public.%I', 'solo_propietario_'||c, t);
        if c = 'insert' then
          execute format('create policy %I on public.%I as restrictive for insert to authenticated with check (public.puede_escribir())', 'solo_propietario_'||c, t);
        elsif c = 'update' then
          execute format('create policy %I on public.%I as restrictive for update to authenticated using (public.puede_escribir()) with check (public.puede_escribir())', 'solo_propietario_'||c, t);
        else
          execute format('create policy %I on public.%I as restrictive for delete to authenticated using (public.puede_escribir())', 'solo_propietario_'||c, t);
        end if;
      end loop;
    end if;
  end loop;
end $$;

-- PDFs del playbook
drop policy if exists "solo_propietario_playbooks_w" on storage.objects;
create policy "solo_propietario_playbooks_w" on storage.objects as restrictive for all to authenticated
  using (bucket_id <> 'playbooks' or public.puede_escribir())
  with check (bucket_id <> 'playbooks' or public.puede_escribir());
