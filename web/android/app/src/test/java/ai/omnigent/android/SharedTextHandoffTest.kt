package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The retention half of the OS-share TEXT handoff. The regression these cover
 * is a signed-out cold share: the first pinned-origin document redirects to
 * `/login` without ever mounting a subscriber, so a shell that drops its copy
 * on the first emit loses the user's text outright.
 */
class SharedTextHandoffTest {
    @Test
    fun `a queued share is delivered once the page is ready`() {
        val handoff = SharedTextHandoff()

        handoff.queue("hello")

        val delivery = handoff.nextDelivery()
        assertEquals("hello", delivery?.text)
        assertTrue(handoff.isPending)
    }

    @Test
    fun `an unacknowledged share survives the page load it was emitted into`() {
        // The signed-out cold share: delivery 1 lands on the document that
        // redirects to /login and reaches no subscriber, so nothing
        // acknowledges. Delivery 2 (the /login document) must still carry it.
        val handoff = SharedTextHandoff()
        handoff.queue("shared while signed out")

        val first = handoff.nextDelivery()
        val second = handoff.nextDelivery()

        assertEquals("shared while signed out", first?.text)
        assertEquals("shared while signed out", second?.text)
        assertTrue(handoff.isPending)
    }

    @Test
    fun `a retry reuses the delivery id so the page can dedup receipt`() {
        val handoff = SharedTextHandoff()
        handoff.queue("hello")

        assertEquals(handoff.nextDelivery()?.id, handoff.nextDelivery()?.id)
    }

    @Test
    fun `acknowledgement stops redelivery`() {
        val handoff = SharedTextHandoff()
        handoff.queue("hello")
        handoff.nextDelivery()

        handoff.acknowledge()

        assertFalse(handoff.isPending)
        assertNull(handoff.nextDelivery())
    }

    @Test
    fun `retention is bounded so a web build that never acknowledges cannot pin a share`() {
        val handoff = SharedTextHandoff(maxRetries = 2)
        handoff.queue("hello")

        assertEquals("hello", handoff.nextDelivery()?.text)
        assertEquals("hello", handoff.nextDelivery()?.text)

        assertNull(handoff.nextDelivery())
        assertFalse(handoff.isPending)
    }

    @Test
    fun `a fresh share replaces its predecessor and resets the retry budget`() {
        val handoff = SharedTextHandoff(maxRetries = 1)
        handoff.queue("first")
        val first = handoff.nextDelivery()
        assertNull(handoff.nextDelivery()) // budget spent

        handoff.queue("second")

        val second = handoff.nextDelivery()
        assertEquals("second", second?.text)
        assertNotEquals(first?.id, second?.id)
    }

    @Test
    fun `nothing is pending before a share arrives`() {
        val handoff = SharedTextHandoff()

        assertFalse(handoff.isPending)
        assertNull(handoff.nextDelivery())
    }

    @Test
    fun `an empty share is ignored and leaves a pending one intact`() {
        val handoff = SharedTextHandoff()
        handoff.queue("hello")

        handoff.queue("")

        assertEquals("hello", handoff.nextDelivery()?.text)
    }

    @Test
    fun `the default bound covers the signed-out login round trip`() {
        // Two deliveries is what that flow actually costs; the default must
        // leave headroom rather than expire mid-flight.
        assertTrue(SharedTextHandoff.DEFAULT_MAX_RETRIES >= 2)
    }
}
