package ai.omnigent.android

/**
 * Retention state for one OS-share TEXT payload (an ACTION_SEND whose
 * EXTRA_TEXT the OS delivered with no attached file) on its way to the web
 * layer — the text-side counterpart of the file handoff's
 * `pendingSharedFilesJson` / `onSharedFilesAcknowledged` protocol in
 * [MainActivity].
 *
 * The shell used to clear its pending text the instant it emitted it, on the
 * assumption that `shareIntake.ts` persists it to `sessionStorage`
 * synchronously inside the emit callback. That holds only when a subscriber
 * exists. On a share taken while signed out it does not: the first
 * pinned-origin document finds a dead session, `main.tsx` sees
 * `isLoginRedirectPending()` and never mounts React, so the injected bridge
 * buffers the text in a per-document closure that dies with the document when
 * `/login` replaces it — and the shell had already dropped its only copy.
 *
 * So delivery is at-least-once here: the payload is retained and re-delivered
 * on each pinned-origin `onPageReady` until the web layer acknowledges it
 * ([acknowledge]). Each delivery carries a [Delivery.id] so the injected
 * bridge script can make *receipt* at-most-once, which keeps a web build too
 * old to acknowledge from appending the same text twice. [maxRetries] bounds
 * retention so a build with no shared-text intake at all cannot pin a stale
 * share for the life of the process; there is no user-facing expiry.
 *
 * Not thread-safe: share intents and page-load callbacks both arrive on the
 * main thread, matching the file-side fields this mirrors.
 */
class SharedTextHandoff(
    private val maxRetries: Int = DEFAULT_MAX_RETRIES,
) {
    /** One attempt to hand [text] to the page, tagged for receipt dedup. */
    data class Delivery(
        val text: String,
        val id: String,
    )

    private var text: String? = null
    private var id: String? = null
    private var retries = 0
    private var sequence = 0L

    /** Whether a payload is still waiting to be acknowledged. */
    val isPending: Boolean
        get() = text != null

    /**
     * Take ownership of a freshly-shared [text], replacing (and abandoning)
     * any still-unacknowledged predecessor.
     *
     * Replacing rather than merging matches the file side: merging two
     * unconsumed shares is the web layer's durable slot's job
     * (`persistPendingShareText` appends), so merging here too would
     * double-count the overlap. An empty share is ignored and leaves any
     * pending payload intact.
     */
    fun queue(text: String) {
        if (text.isEmpty()) return
        this.text = text
        sequence += 1
        id = "t$sequence"
        retries = 0
    }

    /**
     * The next delivery attempt, or null when there is nothing pending or the
     * retry bound has been reached — in which case the payload is dropped
     * here, so a caller that keeps polling does not keep re-arming it.
     *
     * Callers gate on their own page/origin readiness BEFORE calling this: a
     * skipped attempt must not consume a retry, because the common reason to
     * skip (the WebView is parked off the pinned origin mid-login) is exactly
     * the situation this retention exists to survive.
     */
    fun nextDelivery(): Delivery? {
        val pending = text ?: return null
        val deliveryId = id ?: return null
        if (retries >= maxRetries) {
            clear()
            return null
        }
        retries += 1
        return Delivery(text = pending, id = deliveryId)
    }

    /**
     * The web layer has durably taken ownership of the pending payload (see
     * `shareIntake.ts`'s `receiveNativeSharedText`, which acknowledges only
     * after its `sessionStorage` write actually succeeded). Stop retrying.
     */
    fun acknowledge() {
        clear()
    }

    private fun clear() {
        text = null
        id = null
        retries = 0
    }

    companion object {
        /**
         * Deliveries to attempt before giving up on an unacknowledged share.
         *
         * The signed-out cold share costs two: the document that redirects to
         * `/login` without mounting React, and the `/login` document that
         * mounts, persists and acknowledges. An off-origin IdP hop costs none
         * (the caller skips the attempt). Five leaves room for an extra
         * same-origin bounce and matches the file side's bound.
         */
        const val DEFAULT_MAX_RETRIES = 5
    }
}
