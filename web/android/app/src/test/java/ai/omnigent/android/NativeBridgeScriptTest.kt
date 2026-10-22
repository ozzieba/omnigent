package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The injected facade's share-text contract. Asserted against the script
 * source because the shell has no JS engine in unit tests; the behaviour it
 * guards is exercised end to end on a device (see the share acceptance
 * runbook). Same approach as [WorkspaceChromeScriptTest].
 */
class NativeBridgeScriptTest {
    private val source = NativeBridgeScript.source

    @Test
    fun `the facade lets the web layer acknowledge a text share`() {
        // Without this, SharedTextHandoff retries to its bound on every page
        // load because nothing can ever tell it the share landed.
        assertTrue(source.contains("acknowledgeSharedText()"))
        assertTrue(source.contains("""post({ method: "sharedTextReceived" })"""))
    }

    @Test
    fun `the text emit accepts a delivery id`() {
        assertTrue(source.contains("value(text, deliveryId)"))
    }

    @Test
    fun `a redelivery already handed to a subscriber is dropped`() {
        // Delivery is at-least-once, so the ledger is what keeps a retry from
        // appending the same text into the composer twice.
        assertTrue(source.contains("wasSharedTextDelivered(deliveryId)"))
        assertTrue(source.contains("omnigent:nativeSharedTextDelivered"))
    }

    @Test
    fun `the ledger records at hand-off, not on arrival`() {
        // Text buffered for a subscriber that never appears (the /login
        // redirect) must stay eligible for the next page load's retry, so the
        // only place that records a delivery is the hand-off helper.
        assertEquals(1, source.windowed(RECORD.length).count { it == RECORD })
        val beforeRecord = source.substringBefore(RECORD)
        assertTrue(beforeRecord.substringAfterLast(HAND_OFF).isBlank())
        // ...and the buffering branch stores the id for that later hand-off
        // rather than recording it now.
        assertTrue(source.contains("pendingSharedText = { text: text, id: deliveryId };"))
    }

    @Test
    fun `the ledger survives a sessionStorage that throws`() {
        // A locked-down WebView must degrade to plain at-least-once delivery,
        // not break the facade for every other bridge method.
        val reader = source.substringAfter("const deliveredIds = () => {").substringBefore("};")
        assertTrue(reader.contains("try {"))
        assertTrue(reader.contains("catch (_) { return []; }"))
    }

    private companion object {
        /** The ledger write. Its one and only call site is [HAND_OFF]. */
        const val RECORD = "markSharedTextDelivered(id);"
        const val HAND_OFF = "const handSharedText = (callbacks, text, id) => {"
    }
}
