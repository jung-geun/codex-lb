import { authenticateOmpRequest, getOmpManifestAuthorized } from "@/app/api/v1/_helpers/ompApi";

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
  try {
    return Response.json(await getOmpManifestAuthorized(key), {
      headers: { "Cache-Control": "private, no-store" },
    });
  } catch {
    return Response.json({ error: "model_manifest_unavailable" }, { status: 500 });
  }
}
