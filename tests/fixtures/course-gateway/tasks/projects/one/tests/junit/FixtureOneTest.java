import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;
final class FixtureOneTest {
    @Test void zero() { assertEquals(0, FixtureOne.value(0)); }
    @Test void positive() { assertEquals(7, FixtureOne.value(7)); }
    @Test void negative() { assertEquals(-3, FixtureOne.value(-3)); }
}
