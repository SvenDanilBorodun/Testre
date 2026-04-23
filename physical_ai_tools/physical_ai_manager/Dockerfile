# Build stage
FROM node:22 AS build
WORKDIR /app

# Cloud training env vars (baked into React build)
ARG REACT_APP_SUPABASE_URL
ARG REACT_APP_SUPABASE_ANON_KEY
ARG REACT_APP_CLOUD_API_URL
# App mode: 'student' (Docker student build) or 'web' (public teacher/admin deployment)
ARG REACT_APP_MODE=student
ENV REACT_APP_MODE=${REACT_APP_MODE}
# Policy allowlist — student build bakes 'act' only. Admin/web build overrides
# with the full comma-list to expose every policy. Enforced again server-side.
ARG REACT_APP_ALLOWED_POLICIES=act
ENV REACT_APP_ALLOWED_POLICIES=${REACT_APP_ALLOWED_POLICIES}
# Build identifier (e.g. short git sha). Baked into the bundle and written to
# build/version.json so the running app can detect a new image and self-reload.
ARG REACT_APP_BUILD_ID=dev
ENV REACT_APP_BUILD_ID=${REACT_APP_BUILD_ID}

COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
RUN printf '{"buildId":"%s","builtAt":"%s"}\n' "${REACT_APP_BUILD_ID}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > build/version.json

# Production stage
FROM nginx:1.27.5-alpine
COPY --from=build /app/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
