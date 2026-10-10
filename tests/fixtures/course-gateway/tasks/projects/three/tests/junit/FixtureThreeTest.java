import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;
final class FixtureThreeTest {
    @Test void zero() { assertEquals(0, FixtureThree.value(0)); }
    @Test void positive() { assertEquals(21, FixtureThree.value(7)); }
    @Test void negative() { assertEquals(-9, FixtureThree.value(-3)); }
}
