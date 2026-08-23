// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

/// @notice The patched counterpart to Vault.sol: state is updated *before*
/// the external call (checks-effects-interactions), so a reentrant call
/// from the recipient's `receive()`/`fallback()` sees a zero balance and
/// `require(amount > 0, ...)` reverts it. Same public interface as
/// Vault.sol on purpose, so test/Exploit.t.sol can run the identical
/// attack against both and show one fails closed.
contract VaultFixed {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "nothing to withdraw");

        balances[msg.sender] = 0;

        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");
    }

    function balanceOf(address who) external view returns (uint256) {
        return balances[who];
    }

    receive() external payable {}
}
