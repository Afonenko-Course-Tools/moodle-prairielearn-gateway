import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;
final class FixtureTwoTest {
    @Test void zero() { assertEquals(0, FixtureTwo.value(0)); }
    @Test void positive() { assertEquals(14, FixtureTwo.value(7)); }
    @Test void negative() { assertEquals(-6, FixtureTwo.value(-3)); }
}
