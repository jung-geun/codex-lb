import { z } from "zod";
import { requireManagementAuth } from "@/lib/api/requireManagementAuth";
import { getConnectionQuotaCapacity } from "@/lib/services/quotaCapacity";

const idSchema = z.string().trim().min(1).max(200).regex(/^[A-Za-z0-9._:-]+$/);
type Context = { params: Promise<{ id: string }> };

export async function GET(request: Request, context: Context) {
  const authError = await requireManagementAuth(request);
  if (authError) return authError;
  const parsed = idSchema.safeParse((await context.params).id);
  if (!parsed.success) return Response.json({ error: "Invalid connection id" }, { status: 400 });
  const result = getConnectionQuotaCapacity(parsed.data);
  if (!result) return Response.json({ error: "Connection not found" }, { status: 404 });
  return Response.json(result, { headers: { "Cache-Control": "no-store" } });
}
