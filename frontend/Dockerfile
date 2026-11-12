FROM nginx:1.28.0-alpine
ENV MEDIA_ORIGIN=""
COPY frontend/nginx/studio.conf /etc/nginx/templates/default.conf.template
COPY frontend/login/ /usr/share/nginx/html/login/
EXPOSE 8080
