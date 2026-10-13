FROM ghcr.io/gitroomhq/postiz-app@sha256:d2b37802e96757729fdea2d3d71287f841c2b619faa1cb4c21dac07df657fde1
COPY . /adapter/
RUN node /adapter/patch-image.cjs
ENV NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=3072
ENV MAIN_URL=http://localhost:4007 FRONTEND_URL=http://localhost:4007 NEXT_PUBLIC_BACKEND_URL=http://localhost:4007/api
ENV NEXT_PUBLIC_UPLOAD_DIRECTORY=/uploads NEXT_PUBLIC_UPLOAD_STATIC_DIRECTORY=/uploads IS_GENERAL=true NOT_SECURED=true
RUN pnpm --filter ./apps/backend build && pnpm --filter ./apps/orchestrator build
RUN pnpm --filter ./apps/frontend build
