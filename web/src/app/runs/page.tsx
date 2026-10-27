import { redirect } from "next/navigation";

/**
 * `/runs` was the Collection page in all but name: the nav said "Collection"
 * and pointed here, while `/collection` returned 404. One canonical route now
 * holds the answer to "how fresh is this?", and this path keeps working for
 * anything that bookmarked it.
 */
export default function RunsPage() {
  redirect("/collection");
}
