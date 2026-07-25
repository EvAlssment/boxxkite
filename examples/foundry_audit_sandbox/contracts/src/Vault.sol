// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

/// @notice Toy, EVMbench-style vulnerable vault: a classic reentrancy bug
/// (external call before the internal balance is zeroed). Intentionally
/// insecure -- for boxkite's Foundry/Anvil audit-sandbox example only,
/// never a real contract. See ../../README.md for the full "detect / patch
/// / exploit" story this contract, VaultFixed.sol, and Attacker.sol
/// together demonstrate.
contract Vault {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    /// @dev VULNERABLE: sends ETH via a raw `call` (which hands control to
    /// the recipient) *before* zeroing `balances[msg.sender]` -- the
    /// textbook checks-effects-interactions violation.
    function withdraw() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "nothing to withdraw");

        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");

        balances[msg.sender] = 0;
    }

    function balanceOf(address who) external view returns (uint256) {
        return balances[who];
    }

    receive() external payable {}
}
