// Referral links shared by creators: /r/{code} forwards the code as a
// ?ref= query param to the pricing section. The site is a static export,
// so attribution persistence is handled client-side: the pricing
// component asks the visitor for consent and only then stores the
// vx_ref cookie, which it appends as `reference_id` on the Polar
// checkout links. Polar propagates that metadata to the
// Order/Subscription seen by the billing webhook.
// Runs as a Cloudflare Pages Function.

const REF_RE = /^[a-z0-9][a-z0-9_-]{1,31}$/;

type Ctx = {
  request: Request;
  params: Record<string, string | string[]>;
};

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const raw = ctx.params.code;
  const code = Array.isArray(raw) ? raw[0] : raw ?? "";
  const url = new URL("/", ctx.request.url);
  if (REF_RE.test(code)) url.searchParams.set("ref", code);
  url.hash = "pricing";
  return Response.redirect(url.toString(), 302);
};
