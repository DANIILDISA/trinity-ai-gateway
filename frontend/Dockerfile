# Multi-stage Next.js build.
#   builder → produces .next/standalone via `output: "standalone"` in next.config.js
#   dev     → used by docker-compose.yml (target: dev); mounts source + hot-reload
#   runner  → default final stage; used by docker-compose.prod.yml. Minimal Node
#             runtime, non-root user, only the standalone output + static assets.

FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build


FROM node:20-alpine AS dev
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]


FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Drop root before running the Node process.
RUN addgroup -S nextjs && adduser -S nextjs -G nextjs

# Standalone server bundle (includes a minimal node_modules pruned by Next.js).
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
# Build-output static assets — standalone server expects them at /app/.next/static.
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static
# Public assets (e.g. /public/locales).
COPY --from=builder --chown=nextjs:nextjs /app/public ./public

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
