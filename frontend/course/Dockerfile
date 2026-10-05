# The official stable Alpine image is pinned to its linux/amd64 digest.
FROM nginx:stable-alpine@sha256:8f84ed99befc3891b8f329c5c202785278a2cfb7c25107d57fb2a134a3117433
COPY nginx.conf /etc/nginx/nginx.conf
COPY backend-proxy.conf /etc/nginx/backend-proxy.conf
COPY public/ /usr/share/nginx/html/
RUN nginx -t && chown -R nginx:nginx /tmp/client_temp /tmp/proxy_temp /tmp/nginx.pid
USER nginx
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
