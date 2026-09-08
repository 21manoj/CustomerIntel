# The browser app, built and served as static files — `customerintelv1-web` in
# deploy/docker-compose.customerintelv1.yml.
#
# The build runs HERE, in the image, not on the box: the EC2 host has no Node
# and should not need one, and `docker compose up -d --build` (already the one
# command deploy_ec2.sh runs) then covers the frontend too — no extra step for
# anyone to remember or forget.

FROM node:22-alpine AS build
WORKDIR /app

# Separate layer so a source-only change does not reinstall the tree.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# `npm run build` = tsc -b && vite build → dist/. A type error fails the image
# build, which fails the deploy — a broken frontend never reaches the box.
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/index.html || exit 1
