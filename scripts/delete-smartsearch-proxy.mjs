import db from "/app/db.js";
import Access from "/app/lib/access.js";
import internalProxyHost from "/app/internal/proxy-host.js";
import ProxyHost from "/app/models/proxy_host.js";

const DOMAIN = "smartsearch-mcp-home.172906573.xyz";
const OWNER_USER_ID = 1;
const CERTIFICATE_ID = 2;
const hasDomain = (row) =>
    Array.isArray(row.domain_names) &&
    row.domain_names.some(
        (name) => String(name).toLowerCase() === DOMAIN,
    );

async function main() {
    const access = new Access();
    await access.load(true);
    access.token.set("attrs", { id: OWNER_USER_ID });

    const rows = await ProxyHost.query().where("is_deleted", 0);
    const matches = rows.filter(hasDomain);
    if (matches.length === 0) {
        console.log(JSON.stringify({ status: "absent", domain: DOMAIN }));
        return;
    }
    if (matches.length > 1) {
        throw new Error("multiple active proxy hosts contain target domain");
    }

    const row = matches[0];
    const exactDomain =
        row.domain_names.length === 1 &&
        String(row.domain_names[0]).toLowerCase() === DOMAIN;
    const managedTarget =
        exactDomain &&
        row.forward_scheme === "http" &&
        row.forward_host === "smartsearch-mcp" &&
        Number(row.forward_port) === 8000 &&
        Number(row.certificate_id) === CERTIFICATE_ID;
    if (!managedTarget) {
        throw new Error(
            "target domain exists but is not the managed smartsearch proxy host",
        );
    }

    await internalProxyHost.delete(access, {
        id: row.id,
        reason: "smartsearch-mcp rollback",
    });
    console.log(
        JSON.stringify({
            status: "deleted",
            id: row.id,
            domain: DOMAIN,
        }),
    );
}

try {
    await main();
} catch (error) {
    const message = String(error?.message ?? "delete failed")
        .split(/\r?\n/, 1)[0]
        .slice(0, 300);
    console.error(JSON.stringify({ status: "error", message }));
    process.exitCode = 1;
} finally {
    await db().destroy();
}
