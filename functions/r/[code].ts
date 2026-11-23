// Referral links shared by creators: /r/{code} stores the code in a
// cookie and sends the visitor to pricing. The pricing section appends
// it as `metadata[ref]` on the Polar checkout links, which Polar
// propagates to the subscription metadata seen by the billing webhook.
// Runs as a Cloudflare Pages Function (the site is a static export).

const REF_RE = /^[a-z0-9][a-z0-9_-]{1,31}$/;

type Ctx = {
  request: Request;
  params: Record<string, string | string[]>;
};

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const raw = ctx.params.code;
  const code = Array.isArray(raw) ? raw[0] : raw ?? "";
  const target = new URL("/#pricing", ctx.request.url).toString();
  const headers = new Headers({ location: target });
  if (REF_RE.test(code)) {
    headers.append(
      "set-cookie",
      `vx_ref=${code}; Max-Age=${90 * 24 * 3600}; Path=/; SameSite=Lax`
    );
  }
  return new Response(null, { status: 302, headers });
};
