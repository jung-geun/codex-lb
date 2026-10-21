import { authenticateOmpRequest, getOmpQuotaForKey } from "@/app/api/v1/_helpers/ompApi";

export async function OPTIONS() {
  return new Response(null, {
    headers: {
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type",
    },
  });
}

export async function GET(request: Request) {
  const key = await authenticateOmpRequest(request);
  if (!key) return Response.json({ error: "authentication_required" }, { status: 401 });
  const url = new URL(request.url);
  const models = url.searchParams.getAll("model");
  if (models.length !== 1 || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(models[0])) {
    return Response.json({ error: "invalid_model" }, { status: 400 });
  }
  try {
    return Response.json(await getOmpQuotaForKey(key, models[0]), {
      headers: { "Cache-Control": "private, no-store" },
    });
  } catch {
    return Response.json({ error: "quota_unavailable" }, { status: 500 });
  }
}
