# Homelab Approve — развёртывание

Сервис одобрения регистраций. Доступ в Jellyfin гейтится ролью `media`;
этот сервис позволяет админу одобрять/отклонять новых пользователей (страница + письма).

## Что уже сделано на сервере
- Роль `media` в realm `homelab`, выдана нескольким пользователям.
- SSO-плагин Jellyfin требует роль `media` (`<Roles><string>media</string></Roles>`).
- Keycloak-клиент `access-approve` (confidential, standard flow + service account),
  redirect: `https://example.com/_access-approve/oauth2/callback`,
  сервисному аккаунту выдан `manage-users`.

## Шаги развёртывания
1. Скопировать каталог в `/opt/docker/compose/access-approve/` на docker01.
2. Создать `.env` из `.env.example`:
   - `OIDC_CLIENT_SECRET` = секрет клиента `access-approve`
     (`kcadm get clients/<id>/client-secret -r homelab`).
   - `SIGN_KEY`, `FLASK_SECRET` = случайные строки (`openssl rand -hex 32`).
   - SMTP_* = параметры почты No Reply (если письма нужны сразу).
3. `docker compose up -d --build`
4. Добавить в Caddyfile (внутрь блока `example.com { ... }`, ДО `handle { reverse_proxy jellyfin:8096 }`):

   ```
   @approve path /_access-approve /_access-approve/*
   handle @approve {
       reverse_proxy access-approve:8090
   }
   ```
   затем `docker exec caddy caddy reload --config /etc/caddy/Caddyfile` (или перезапуск caddy).

## Проверка
- Открыть `https://example.com/_access-approve/` → редирект на вход Keycloak → после входа админом видно список ожидающих.
- Зарегистрировать тестового пользователя → должен появиться в списке (и прийти письмо, если SMTP настроен).
- «Одобрить» → у пользователя появляется роль `media` → он может войти в Jellyfin.

## Откат
- Caddy: убрать блок `@approve`, reload.
- `docker compose down` в каталоге сервиса.
- Роль-гейтинг: вернуть `<Roles />` в SSO-Auth.xml (есть бэкап `.bak-rolegate-*`) и перезапустить jellyfin.
