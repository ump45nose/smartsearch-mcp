import db from "/app/db.js";
import Access from "/app/lib/access.js";
import apiValidator from "/app/lib/validator/api.js";
import internalProxyHost from "/app/internal/proxy-host.js";
import ProxyHost from "/app/models/proxy_host.js";
import {
    getCompiledSchema,
    getValidationSchema,
} from "/app/schema/index.js";

const DOMAIN = "smartsearch-mcp-home.172906573.xyz";
const OWNER_USER_ID = 1;
const CERTIFICATE_ID = 2;
const ADVANCED_CONFIG = `location ^~ /codex-oauth-callback/ {
    access_log off;
    error_log /dev/null crit;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Scheme $scheme;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_connect_timeout 10s;
    proxy_read_timeout 30s;
    proxy_send_timeout 30s;
    proxy_pass http://192.168.31.201:5555$request_uri;
}

location / {
    access_log off;
    error_log /dev/null crit;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
    proxy_connect_timeout 30s;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;
    send_timeout 900s;
    include conf.d/include/proxy.conf;
}`;

const normalize = (value) =>
    String(value ?? "").replace(/\r\n/g, "\n").trim();
const hasDomain = (row) =>
    Array.isArray(row.domain_names) &&
    row.domain_names.some(
        (name) => String(name).toLowerCase() === DOMAIN,
    );

function assertDesired(row) {
    const checks = {
        domain_names:
            Array.isArray(row.domain_names) &&
            row.domain_names.length === 1 &&
            String(row.domain_names[0]).toLowerCase() === DOMAIN,
        forward_scheme: row.forward_scheme === "http",
        forward_host: row.forward_host === "smartsearch-mcp",
        forward_port: Number(row.forward_port) === 8000,
        certificate_id: Number(row.certificate_id) === CERTIFICATE_ID,
        ssl_forced: row.ssl_forced === true,
        http2_support: row.http2_support === true,
        caching_enabled: row.caching_enabled === false,
        allow_websocket_upgrade: row.allow_websocket_upgrade === false,
        block_exploits: row.block_exploits === false,
        access_list_id: Number(row.access_list_id) === 0,
        hsts_enabled: row.hsts_enabled === false,
        hsts_subdomains: row.hsts_subdomains === false,
        trust_forwarded_proto: row.trust_forwarded_proto === false,
        enabled: row.enabled === true,
        locations:
            row.locations == null ||
            (Array.isArray(row.locations) && row.locations.length === 0),
        advanced_config:
            normalize(row.advanced_config) === normalize(ADVANCED_CONFIG),
        nginx_online: row.meta?.nginx_online === true,
    };
    const drift = Object.entries(checks)
        .filter(([, ok]) => !ok)
        .map(([key]) => key);
    if (drift.length) {
        throw new Error(`existing host drift: ${drift.join(",")}`);
    }
}

async function main() {
    const access = new Access();
    await access.load(true);
    access.token.set("attrs", { id: OWNER_USER_ID });

    const rows = await ProxyHost.query().where("is_deleted", 0);
    const matches = rows.filter(hasDomain);
    if (matches.length > 1) {
        throw new Error("multiple active proxy hosts contain target domain");
    }
    if (matches.length === 1) {
        assertDesired(matches[0]);
        console.log(
            JSON.stringify({
                status: "unchanged",
                id: matches[0].id,
                domain: DOMAIN,
            }),
        );
        return;
    }

    await getCompiledSchema();
    const payload = await apiValidator(
        getValidationSchema("/nginx/proxy-hosts", "post"),
        {
            domain_names: [DOMAIN],
            forward_scheme: "http",
            forward_host: "smartsearch-mcp",
            forward_port: 8000,
            access_list_id: 0,
            certificate_id: CERTIFICATE_ID,
            ssl_forced: true,
            caching_enabled: false,
            block_exploits: false,
            advanced_config: ADVANCED_CONFIG,
            meta: {},
            allow_websocket_upgrade: false,
            http2_support: true,
            enabled: true,
            locations: [],
            hsts_enabled: false,
            hsts_subdomains: false,
            trust_forwarded_proto: false,
        },
    );

    const created = await internalProxyHost.create(access, payload);
    const verified = await ProxyHost.query().findById(created.id);
    if (!verified) {
        throw new Error("created proxy host cannot be reloaded");
    }
    assertDesired(verified);
    console.log(
        JSON.stringify({
            status: "created",
            id: verified.id,
            domain: DOMAIN,
        }),
    );
}

try {
    await main();
} catch (error) {
    const message = String(error?.message ?? "create failed")
        .split(/\r?\n/, 1)[0]
        .slice(0, 300);
    console.error(JSON.stringify({ status: "error", message }));
    process.exitCode = 1;
} finally {
    await db().destroy();
}
