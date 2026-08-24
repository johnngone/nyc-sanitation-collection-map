FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm install
COPY frontend ./
ARG VITE_API_BASE_URL=
ARG VITE_BASEMAP_TILEJSON_URL=https://tiles.openfreemap.org/planet
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_BASEMAP_TILEJSON_URL=$VITE_BASEMAP_TILEJSON_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

