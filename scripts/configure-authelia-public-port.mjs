import db from "/app/db.js";
import Access from "/app/lib/access.js";
import internalProxyHost from "/app/internal/proxy-host.js";
import ProxyHost from "/app/models/proxy_host.js";

const DOMAIN = "authelia-home.172906573.xyz";
const OWNER_USER_ID = 1;
const CERTIFICATE_ID = 2;
const REMOVE = process.argv.includes("--remove");
const ADVANCED_CONFIG = `proxy_set_header Accept-Encoding "";
proxy_redirect https://authelia-home.172906573.xyz/ https://authelia-home.172906573.xyz:28443/;
sub_filter_once off;
sub_filter_types application/json;
sub_filter '"jwks_uri":"https://authelia-home.172906573.xyz/' '"jwks_uri":"https://authelia-home.172906573.xyz:28443/';
sub_filter '"authorization_endpoint":"https://authelia-home.172906573.xyz/' '"authorization_endpoint":"https://authelia-home.172906573.xyz:28443/';
sub_filter '"token_endpoint":"https://authelia-home.172906573.xyz/' '"token_endpoint":"https://authelia-home.172906573.xyz:28443/';
sub_filter '"introspection_endpoint":"https://authelia-home.172906573.xyz/' '"introspection_endpoint":"https://authelia-home.172906573.xyz:28443/';
sub_filter '"revocation_endpoint":"https://authelia-home.172906573.xyz/' '"revocation_endpoint":"https://authelia-home.172906573.xyz:28443/';
sub_filter '"device_authorization_endpoint":"https://authelia-home.172906573.xyz/' '"device_authorization_endpoint":"https://authelia-home.172906573.xyz:28443/';
sub_filter '"pushed_authorization_request_endpoint":"https://authelia-home.172906573.xyz/' '"pushed_authorization_request_endpoint":"https://authelia-home.172906573.xyz:28443/';`;

const normalize = (value) =>
    String(value ?? "").replace(/\r\n/g, "\n").trim();
const hasDomain = (row) =>
    Array.isArray(row.domain_names) &&
    row.domain_names.some(
        (name) => String(name).toLowerCase() === DOMAIN,
    );

function assertTarget(row) {
    const exactDomain =
        Array.isArray(row.domain_names) &&
        row.domain_names.length === 1 &&
        String(row.domain_names[0]).toLowerCase() === DOMAIN;
    const managedTarget =
        exactDomain &&
        row.forward_scheme === "http" &&
        row.forward_host === "authelia" &&
        Number(row.forward_port) === 28991 &&
        Number(row.certificate_id) === CERTIFICATE_ID &&
        row.enabled === true;
    if (!managedTarget) {
        throw new Error("Authelia proxy host does not match the expected target");
    }
}

async function main() {
    const access = new Access();
    await access.load(true);
    access.token.set("attrs", { id: OWNER_USER_ID });

    const rows = await ProxyHost.query().where("is_deleted", 0);
    const matches = rows.filter(hasDomain);
    if (matches.length !== 1) {
        throw new Error("expected exactly one active Authelia proxy host");
    }

    const row = matches[0];
    assertTarget(row);
    const current = normalize(row.advanced_config);
    const desired = normalize(ADVANCED_CONFIG);

    if (REMOVE) {
        if (!current) {
            console.log(JSON.stringify({ status: "absent", id: row.id }));
            return;
        }
        if (current !== desired) {
            throw new Error("refusing to remove unrecognized advanced config");
        }
    } else {
        if (current === desired) {
            console.log(JSON.stringify({ status: "unchanged", id: row.id }));
            return;
        }
        if (current) {
            throw new Error("refusing to overwrite existing advanced config");
        }
    }

    await internalProxyHost.update(access, {
        id: row.id,
        advanced_config: REMOVE ? "" : ADVANCED_CONFIG,
    });
    const verified = await ProxyHost.query().findById(row.id);
    assertTarget(verified);
    const expected = REMOVE ? "" : desired;
    if (normalize(verified.advanced_config) !== expected) {
        throw new Error("Authelia advanced config verification failed");
    }
    console.log(
        JSON.stringify({
            status: REMOVE ? "removed" : "configured",
            id: row.id,
        }),
    );
}

try {
    await main();
} catch (error) {
    const message = String(error?.message ?? "configuration failed")
        .split(/\r?\n/, 1)[0]
        .slice(0, 300);
    console.error(JSON.stringify({ status: "error", message }));
    process.exitCode = 1;
} finally {
    await db().destroy();
}
