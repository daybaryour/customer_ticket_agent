# Stage 1: build the React/Vite bundle.
FROM node:20-alpine AS build

WORKDIR /app

# Install from the lockfile so container builds match local installs.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# Stage 2: serve the static bundle with nginx and proxy /api/ to the backend.
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
