FROM nginx:1.28.0-alpine
ENV MEDIA_ORIGIN=""
COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template
COPY frontend/登录.html frontend/登录.js /usr/share/nginx/html/
EXPOSE 8080
