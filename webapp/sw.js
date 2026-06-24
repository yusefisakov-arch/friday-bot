// Минимальный service worker — нужен для установки PWA. Без кэша, чтобы данные были свежими.
self.addEventListener("install", function (e) { self.skipWaiting(); });
self.addEventListener("activate", function (e) { self.clients.claim(); });
self.addEventListener("fetch", function (e) { /* сеть напрямую */ });
